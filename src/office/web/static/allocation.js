// Allocation: the machine-wide scheduler across repositories. Everything shown comes from the
// service's scheduler projection; controls send commands and the view changes only when the
// server's state does. No value is invented: unmeasured host and unknown quota say so.

export const PRIORITIES = ["urgent", "high", "normal", "low"];
const GLOBAL = "alloc:global";
const ROLE = { run: "Orchestrator", task: "Task", issue: "New run" };

// The server's ready order (office.scheduler.ready_order): paused last, demoted just before,
// then highest score, then earliest enqueued, then id. Deltas do not keep object order, so it is re-derived.
export function readyOrder(entries) {
  const key = (e) => [e.paused ? 1 : 0, e.demoted_seq !== null && e.demoted_seq !== undefined ? 1 : 0,
    e.demoted_seq || 0, -((e.score || {}).total || 0), e.enqueued_at || "", e.id];
  return [...entries].sort((a, b) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < ka.length; i += 1) if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
    return 0;
  });
}

export function group(e) {
  if (e.paused || (e.demoted_seq !== null && e.demoted_seq !== undefined)) return "held";
  if (e.protected || (e.kind === "run" && e.decision === "admit")) return "active";
  return "ready";
}

const runOf = (state, e) => (e.run_id ? state.entities.runs[`run:${e.run_id}`] || null : null);

// {allowed, reason} for a control on an entry, from the run's capability (older runtimes are read-only).
function control(ctx, e, kind) {
  if (ctx.state.freshness && (ctx.state.freshness.office || {}).state !== "live") {
    return { allowed: false, reason: "Office data is not live; commands are refused" };
  }
  if (!e.run_id) return { allowed: true, reason: null };
  const run = runOf(ctx.state, e);
  if (!run) return { allowed: false, reason: "run not found" };
  const c = (run.controls || {})[kind];
  return c || { allowed: false, reason: "this runtime does not report the control" };
}

const target = (e) => (e.run_id ? { run_id: e.run_id, ...(e.task_id ? { task_id: e.task_id } : {}) } : { item: e.id });

function busy(ctx, kind, subject) { return ctx.inFlight(kind, subject); }

// A sent command blocks a second send with aria-disabled (not `disabled`), so the control keeps focus.

function btn(ctx, e, kind, text, payload, label) {
  const c = control(ctx, e, kind);
  const pending = busy(ctx, kind, `alloc:${e.id}`);
  return ctx.h("button", {
    type: "button", class: "btn", disabled: !c.allowed, "aria-disabled": pending ? "true" : null,
    title: c.reason || (pending ? "sent; waiting for the result" : null),
    "aria-label": `${label} ${e.title || e.id}`, dataset: { key: `${kind}:${text}:${e.id}`, testid: `alloc-${kind}` },
    onclick: () => !busy(ctx, kind, `alloc:${e.id}`) && ctx.send(kind, `alloc:${e.id}`, target(e), payload, {}, `${label} ${e.title || e.id}`),
  }, text);
}

function prioritySelect(ctx, e) {
  const c = control(ctx, e, "set_priority");
  const pending = busy(ctx, "set_priority", `alloc:${e.id}`);
  return ctx.h("select", {
    class: "field", disabled: !c.allowed, "aria-disabled": pending ? "true" : null, title: c.reason, "aria-label": `Priority of ${e.title || e.id}`,
    dataset: { key: `prio:${e.id}`, testid: "alloc-priority" },
    onchange: (ev) => {
      const level = ev.target.value;
      ev.target.value = e.priority; // the server's value stays until its state changes
      if (busy(ctx, "set_priority", `alloc:${e.id}`)) return;
      ctx.send("set_priority", `alloc:${e.id}`, target(e), { level }, {}, `Priority ${level} ${e.title || e.id}`);
    },
  }, PRIORITIES.includes(e.priority) ? null : ctx.h("option", { value: "", selected: true, disabled: true }, `${e.priority || "unknown"}`),
  PRIORITIES.map((p) => ctx.h("option", { value: p, selected: p === e.priority }, p)));
}

function score(ctx, e) {
  const s = e.score || { total: null, components: {} };
  const parts = Object.entries(s.components || {}).filter(([, v]) => v);
  return ctx.h("span", { class: "score", dataset: { testid: "alloc-score" }, title: parts.map(([k, v]) => `${k} ${v}`).join(", ") },
    ctx.h("b", { text: s.total === null ? "—" : String(s.total) }),
    ctx.h("span", { class: "comps" }, Object.entries(s.components || {}).map(([k, v]) =>
      ctx.h("span", { class: "comp", dataset: { comp: k }, text: `${k.replace("_", " ")} ${v}` }))));
}

function markers(ctx, e) {
  const out = [];
  if (e.blocks) out.push(ctx.h("span", { class: "pill crit", dataset: { testid: "alloc-critical" }, text: `critical path · unblocks ${e.blocks}` }));
  if (e.protected) out.push(ctx.h("span", { class: "pill prot", text: "protected" }));
  if (e.projected) out.push(ctx.h("span", { class: "pill term", dataset: { testid: "alloc-terminal" }, text: "terminal-started" }));
  if (e.demoted_seq !== null && e.demoted_seq !== undefined) out.push(ctx.h("span", { class: "pill warn", text: "demoted" }));
  if (e.paused) out.push(ctx.h("span", { class: "pill warn", text: "paused" }));
  return out;
}

function row(ctx, e, i) {
  const h = ctx.h;
  const run = runOf(ctx.state, e);
  const repo = run && run.repo ? (run.repo.slug || run.repo.key) : (e.ref ? String(e.ref).split("#")[0] : "—");
  const auto = e.auto_mode || "unknown";
  // Only a known state offers a toggle; "paused" and "unknown" are shown, never guessed into "off".
  const autoBtn = !e.run_id || !["on", "off", "paused"].includes(auto) ? null
    : (auto === "on" ? btn(ctx, e, "set_auto_mode", "Auto off", { mode: "off" }, "Turn auto mode off for")
      : btn(ctx, e, "set_auto_mode", "Resume auto", { mode: "on" }, "Resume auto mode for"));
  const why = run && run.controls && run.controls.runtime && run.controls.runtime.read_only
    ? h("div", { class: "why", dataset: { testid: "alloc-readonly" }, text: (run.controls.pause || {}).reason || "read-only runtime" }) : null;
  return h("div", { class: "arow", role: "row", dataset: { testid: "alloc-row", id: e.id, group: group(e) } },
    h("div", { class: "c n", role: "cell", text: String(i + 1) }),
    h("div", { class: "c work", role: "cell" }, h("div", { class: "t", text: e.title || e.id }),
      h("div", { class: "s", text: e.task_id ? `task ${e.task_id}` : (e.ref || "") }), why),
    h("div", { class: "c", role: "cell", dataset: { testid: "alloc-repo" }, text: repo }),
    h("div", { class: "c mono", role: "cell", dataset: { testid: "alloc-run" }, text: e.run_id ? ctx.shortRun(e.run_id) : "—", title: e.run_id }),
    h("div", { class: "c", role: "cell", dataset: { testid: "alloc-role" }, text: ROLE[e.kind] || e.kind }),
    h("div", { class: "c", role: "cell" }, prioritySelect(ctx, e)),
    h("div", { class: "c", role: "cell" }, score(ctx, e)),
    h("div", { class: "c marks", role: "cell" }, markers(ctx, e)),
    h("div", { class: "c", role: "cell", dataset: { testid: "alloc-auto", mode: auto }, text: `auto ${auto}` }),
    h("div", { class: "c", role: "cell", title: (e.notes || []).join("; ") || null, dataset: { testid: "alloc-decision", decision: e.decision } },
      h("b", { text: e.decision || "—" }), h("div", { class: "s", text: e.reason || "" })),
    h("div", { class: "c ctl", role: "cell" },
      e.paused ? btn(ctx, e, "resume", "Resume", {}, "Resume") : btn(ctx, e, "pause", "Pause", {}, "Pause"),
      btn(ctx, e, "demote", "Demote", {}, "Demote"), autoBtn));
}

const HEAD = ["#", "Work", "Repository", "Run", "Role", "Priority", "Score", "Markers", "Auto", "Decision", "Controls"];

function table(ctx, title, testid, entries, offset, empty) {
  const h = ctx.h;
  return h("section", { class: "alloc-group", dataset: { testid }, "aria-label": title },
    h("h2", { text: `${title} (${entries.length})` }),
    entries.length
      ? h("div", { class: "atable", role: "table", "aria-label": title },
        h("div", { class: "arow head", role: "row" }, HEAD.map((c) => h("div", { class: "c", role: "columnheader", text: c }))),
        entries.map((e, i) => row(ctx, e, offset + i)))
      : h("p", { class: "muted", text: empty }));
}

function sample(ctx, name, s, pressure) {
  const h = ctx.h;
  const measured = s && (s.status === "ok" || s.status === "measured") && typeof s.value === "number";
  const value = !measured ? `${name} unavailable`
    : String(s.unit).startsWith("fraction") ? `${Math.round(s.value * 100)}%`
      : s.unit === "percent" ? `${Math.round(s.value)}%` : `${s.value} ${s.unit || ""}`.trim();
  return h("div", { class: "metric", dataset: { testid: `alloc-${name.toLowerCase()}`, status: measured ? "measured" : "unavailable" } },
    h("div", { class: "v", text: value }),
    h("div", { class: "k", text: measured ? `${name} pressure (${pressure || "ok"})` : `${name}: ${(s && s.status) || "unknown"}; not gating` }));
}

// Provider quota from agent telemetry; a provider with no measured value is unknown, never ok.
export function quotas(state) {
  const by = new Map();
  for (const a of Object.values(state.entities.agents || {})) {
    if (!a.harness) continue;
    const q = (a.telemetry || {}).quota || {};
    const prev = by.get(a.harness);
    if (q.status === "measured" || !prev) by.set(a.harness, { provider: a.harness, status: q.status || "unknown", value: q.value, source: q.source });
  }
  return [...by.values()].sort((a, b) => a.provider.localeCompare(b.provider));
}

export function render(ctx) {
  const h = ctx.h;
  const s = ctx.state;
  if (!s) return h("p", { class: "muted", text: "Loading scheduler…" });
  const sched = s.scalars.scheduler || {};
  const tel = (s.scalars.host || {}).telemetry || {};
  const global = sched.auto_mode || "unknown";
  const entries = readyOrder(Object.values(s.entities.queue || {}));
  const groups = { active: [], ready: [], held: [] };
  for (const e of entries) groups[group(e)].push(e);
  const live = (s.freshness.office || {}).state === "live";
  const pending = busy(ctx, "set_auto_mode", GLOBAL);
  const toggle = global === "on"
    ? { mode: "off", text: "Turn auto mode off", label: "Auto mode off (machine)" }
    : { mode: "on", text: "Resume auto mode", label: "Resume auto mode (machine)" };
  const rec = sched.recommended && sched.recommended.evidence
    ? h("div", { class: "metric", dataset: { testid: "alloc-recommended" } }, h("div", { class: "v", text: String(sched.recommended.value) }),
      h("div", { class: "k", text: `recommended active (${sched.recommended.evidence})` }))
    : h("div", { class: "metric", dataset: { testid: "alloc-recommended", status: "none" } }, h("div", { class: "v", text: "—" }),
      h("div", { class: "k", text: "No recommended agent count: no calibration evidence" }));
  const qs = quotas(s);
  const last = [...ctx.local.values()].filter((l) => String(l.subject).startsWith("alloc:")).pop();
  return [
    h("div", { class: "canvas-head" },
      h("div", {}, h("h1", { text: "Allocation" }), h("p", { class: "sub", text: "One machine-wide scheduler across every repository and run, including runs started from a terminal." })),
      h("div", { class: "auto-global", dataset: { testid: "alloc-auto-global", mode: global } },
        h("span", { class: `pill mode-${global}`, text: `Auto mode ${global}` }),
        h("button", { type: "button", class: `btn${toggle.mode === "on" ? " primary" : ""}`, disabled: !live, "aria-disabled": pending ? "true" : null,
          title: !live ? "Office data is not live; commands are refused" : null, dataset: { key: "alloc-global", testid: "alloc-auto-toggle" },
          onclick: () => !busy(ctx, "set_auto_mode", GLOBAL) && ctx.send("set_auto_mode", GLOBAL, {}, { mode: toggle.mode }, {}, toggle.label) }, toggle.text))),
    h("div", { class: "alloc-panels" },
      h("section", { class: "panel", dataset: { testid: "alloc-host" }, "aria-label": "Host capacity" },
        h("h2", { text: "Host CPU and RAM" }),
        h("div", { class: "metrics" }, sample(ctx, "CPU", tel.cpu, (sched.host || {}).cpu), sample(ctx, "RAM", tel.ram, (sched.host || {}).ram),
          h("div", { class: "metric", dataset: { testid: "alloc-active" } }, h("div", { class: "v", text: String(sched.active ?? "—") }), h("div", { class: "k", text: "running now" })),
          rec)),
      h("section", { class: "panel", dataset: { testid: "alloc-quota" }, "aria-label": "Provider quota" },
        h("h2", { text: "Provider / account quota" }),
        qs.length
          ? h("ul", { class: "quota" }, qs.map((q) => h("li", { dataset: { testid: "alloc-quota-provider", status: q.status } },
            h("b", { text: q.provider }), h("span", { text: q.status === "measured" ? String(q.value) : `quota ${q.status}` }))))
          : h("p", { class: "muted", text: "Quota unknown: no provider reports quota telemetry." }))),
    last ? h("p", { class: "alloc-last", dataset: { testid: "alloc-last-command", status: ctx.receiptState((s.entities.commands[`command:${last.id}`] || {}).status || last.status) },
      text: `${last.label}: ${ctx.receiptState((s.entities.commands[`command:${last.id}`] || {}).status || last.status)}${last.error ? ` (${last.error})` : ""}` }) : null,
    h("div", { class: "alloc-tables" },
      table(ctx, "Active work", "alloc-active-group", groups.active, 0, "Nothing is running."),
      table(ctx, "Ready queue", "alloc-ready-group", groups.ready, groups.active.length, "The ready queue is empty."),
      table(ctx, "Paused and demoted", "alloc-held-group", groups.held, groups.active.length + groups.ready.length, "No paused or demoted work.")),
  ];
}
