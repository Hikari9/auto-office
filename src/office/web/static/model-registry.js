// Models & Routing: a single Office-authored registry projection. The browser
// may request a global user-policy mutation only through settings_set receipts;
// it never opens YAML, guesses live quota or grants adapter trust.
const ui = { query: "", harness: "", tab: "registry", selected: null, expanded: new Set(), confirm: null };
const title = (g) => g.model_id + " · " + g.version;
const routeSpec = (g) => g.harness + "/" + g.model_id;
const show = (v) => v === null || v === undefined ? "Unknown" : String(v);
const defined = (v) => v !== null && v !== undefined;
const caption = (e) => e.technical_state === "available" ? "Catalog route listed; live probe not run"
  : e.technical_state === "not-installed" ? "Harness not installed"
  : e.technical_state.replaceAll("-", " ");

function intentFor(g, d, action) {
  const policy = d.policy || {};
  const denied = policy.denied_models || [];
  const overkill = policy.overkill_rules || [];
  const ceilings = policy.version_ceilings || {};
  const route = routeSpec(g);
  if (action === "deny") {
    return { key: "routing.user_policy.denied_models",
      value: [...denied, route], label: "Deny " + route,
      detail: "This explicit user policy denies all efforts of " + route + " for automatic and manual routing." };
  }
  if (action === "allow") {
    return { key: "routing.user_policy.denied_models",
      value: denied.filter((v) => v !== route), label: "Remove direct denial of " + route,
      detail: "Only this explicitly named model/harness deny is removed. Version ceilings and technical gates remain unchanged." };
  }
  if (action === "manual") {
    return { key: "routing.user_policy.overkill_rules",
      value: [...overkill, { route }], label: "Make " + route + " manual-only",
      detail: "Automatic routing avoids this model. An otherwise permitted explicit manual route may still use it." };
  }
  if (action === "automatic") {
    return { key: "routing.user_policy.overkill_rules",
      value: overkill.filter((r) => !(r && r.route === route && Object.keys(r).length === 1)),
      label: "Remove global manual-only rule from " + route,
      detail: "Only the exact unscoped rule is removed; any other scoped rules remain." };
  }
  if (action === "ceiling") {
    return { key: "routing.user_policy.version_ceilings",
      value: { ...ceilings, [g.lineage]: g.version },
      label: "Deny " + g.lineage + " through " + g.version,
      detail: "This is inclusive: it denies this lineage's generation " + g.version + " and all older versions across harnesses. Newer generations stay permitted." };
  }
  if (action === "unceil") {
    const value = { ...ceilings };
    delete value[g.lineage];
    return { key: "routing.user_policy.version_ceilings", value,
      label: "Clear " + g.lineage + " generation ceiling",
      detail: "This removes only the lineage ceiling. Explicit model denies and technical gates still apply." };
  }
  return null;
}

function evidence(ctx, g, data, opts = {}) {
  const h = ctx.h;
  const editable = opts.editable !== false;
  const rule = routeSpec(g);
  const policy = data.policy || {};
  const directDenied = (policy.denied_models || []).includes(rule);
  const manual = (policy.overkill_rules || []).some((r) => r && r.route === rule && Object.keys(r).length === 1);
  const live = (ctx.state.freshness.office || {}).state === "live";
  const pending = !editable || !live || ctx.inFlight("settings_set", "settings:model-policy");
  const action = (label, which, disabled = false) => h("button", {
    type: "button", class: "btn", disabled: disabled || pending,
    onclick: () => { ui.confirm = { ...intentFor(g, data, which), modelKey: g.key }; ctx.rerender(); },
  }, label);
  const rows = (g.efforts || []).map((e) => h("tr", {},
    h("td", { text: show(e.effort) }),
    h("td", { text: defined(e.benchmark_score) ? String(e.benchmark_score) : "Unknown" }),
    h("td", { text: caption(e) }),
    h("td", { text: show(e.source_name) + " / " + show(e.source_identifier) })));
  return h("div", { class: "model-evidence", dataset: { testid: opts.mobile ? "model-card-evidence" : "model-inspector" } },
    h("h3", { text: title(g) }),
    h("p", { class: "sub", text: g.lineage + " · " + g.harness + " · current generation" }),
    h("dl", { class: "model-facts" },
      h("dt", { text: "User permission" }),
      h("dd", { text: g.user_denied ? "Denied by you" : "Allowed by you" }),
      h("dt", { text: "Installed harness" }),
      h("dd", { text: g.installed ? "Detected" : "Not installed or not verified" }),
      h("dt", { text: "Live quota" }),
      h("dd", { text: "Unknown — not probed on this screen" }),
      h("dt", { text: "Lineage ceiling" }),
      h("dd", { text: g.version_ceiling ? "Through " + g.version_ceiling + " (" + show(g.ceiling_source) + ")" : "None" }),
      h("dt", { text: "Policy source" }),
      h("dd", { text: show(g.user_policy_source) }),
      h("dt", { text: "Run pinning" }),
      h("dd", { text: "Changes affect future/unpinned choices; existing running routes remain pinned" })),
    g.user_denial_reason ? h("p", { class: "model-reason", text: g.user_denial_reason }) : null,
    h("h4", { text: "Effort-specific benchmark & technical evidence" }),
    h("div", { class: "model-effort-scroll" },
      h("table", { class: "model-efforts" },
        h("thead", {}, h("tr", {},
          ...["Effort", "Score", "Technical state", "Source / identifier"].map((s) => h("th", { scope: "col", text: s })))),
        h("tbody", {}, rows))),
    h("p", { class: "model-reason", text: "Benchmark: " + data.index + ". A score never grants harness conformance or trust. Live availability is separately verified at launch." }),
    h("div", { class: "model-actions", "aria-label": "Global user model policy" },
      directDenied ? action("Remove direct denial", "allow") : action("Deny this model", "deny"),
      manual ? action("Remove manual-only", "automatic") : action("Make manual-only", "manual"),
      g.version_ceiling ? action("Clear lineage ceiling", "unceil") : action("Deny this generation and older", "ceiling")),
    !live ? h("p", { class: "model-reason", text: "The Office service is not live. Model policy changes are unavailable." }) : null,
    h("p", { class: "model-reason", text: "Policy controls use the same Office config command/receipt path as the CLI. A failed save does not update the displayed state." }));
}

function confirmation(ctx, data, editable = true) {
  if (!ui.confirm) return null;
  const h = ctx.h;
  const change = ui.confirm;
  const record = data.generations.find((g) => g.key === change.modelKey);
  const stale = !editable || (ctx.state.freshness.office || {}).state !== "live";
  const busy = ctx.inFlight("settings_set", "settings:model-policy");
  const cancel = () => { ui.confirm = null; ctx.rerender(); };
  const submit = () => {
    if (!stale && !busy && record) {
      ctx.send("settings_set", "settings:model-policy",
        { tier: "machine", key: change.key }, { value: change.value }, {}, change.label);
      cancel();
    }
  };
  return h("div", { class: "model-confirm", role: "group", "aria-label": "Confirm model policy edit",
    dataset: { testid: "model-confirm" }, onkeydown: (ev) => { if (ev.key === "Escape") cancel(); } },
    h("h4", { text: "Confirm global policy change" }),
    h("p", { text: change.detail }),
    h("p", { class: "model-reason", text: "Effective setting: " + change.key + ". No catalog records, benchmark evidence or running-task routes will be rewritten." }),
    h("div", { class: "model-actions" },
      h("button", { type: "button", class: "btn primary", disabled: stale || busy || !record, onclick: submit }, "Confirm"),
      h("button", { type: "button", class: "btn", onclick: cancel }, "Cancel")));
}

export function render(ctx, data, editable = true) {
  const h = ctx.h;
  if (!data || !Array.isArray(data.generations)) return h("section", { class: "scard" },
    h("h2", { text: "Models & Routing" }), h("p", { class: "muted", text: "Model registry is unavailable." }));
  const all = data.generations;
  const q = ui.query.trim().toLowerCase();
  const harnesses = [...new Set(all.map((g) => g.harness))].sort();
  const matches = all.filter((g) => (!ui.harness || g.harness === ui.harness) &&
    (!q || [g.model_id, g.lineage, g.harness, g.version].some((v) => String(v || "").toLowerCase().includes(q))));
  const selected = matches.find((g) => g.key === ui.selected) || matches[0] || null;
  if (selected && selected.key !== ui.selected) ui.selected = selected.key;
  const active = ui.tab === "registry";
  const rowButton = (g) => h("button", {
    type: "button", class: "btn", "aria-pressed": String(g.key === ui.selected),
    onclick: () => { ui.selected = g.key; ui.confirm = null; ctx.rerender(); },
  }, "Inspect");
  const rows = matches.map((g) => h("tr", { class: g.key === ui.selected ? "selected" : "",
    dataset: { testid: "model-registry-row", model: g.model_id } },
    h("th", { scope: "row", text: g.model_id }),
    h("td", { text: g.version }),
    h("td", { text: g.harness }),
    h("td", { text: (g.efforts || []).map((e) => e.effort).join(", ") }),
    h("td", { text: g.installed ? "Detected" : "Not installed" }),
    h("td", { text: g.user_denied ? "Denied by you" : "Allowed by you" }),
    h("td", {}, rowButton(g))));
  const cards = matches.map((g) => {
    const open = ui.expanded.has(g.key);
    return h("article", { class: "model-card", dataset: { testid: "model-registry-card", model: g.model_id } },
      h("div", { class: "model-card-heading" },
        h("div", {}, h("h3", { text: title(g) }),
          h("p", { class: "sub", text: g.harness + " · " + (g.efforts || []).map((e) => e.effort).join(" / ") }),
          h("p", { class: "model-reason", text: g.user_denied ? "Denied by you" : "Allowed by you" })),
        h("button", { class: "btn", type: "button", "aria-expanded": String(open),
          onclick: () => { open ? ui.expanded.delete(g.key) : ui.expanded.add(g.key); ctx.rerender(); } },
          open ? "Collapse" : "Expand evidence & policy")),
      open ? evidence(ctx, g, data, { mobile: true, editable }) : null);
  });
  const page = h("section", { class: "scard model-registry", "aria-label": "Models & Routing",
    dataset: { testid: "model-registry" } },
    h("div", { class: "model-heading" },
      h("div", {}, h("h2", { text: "Models & Routing" }),
        h("p", { class: "sub", text: "Registry truth and your permission are separate. Latest two released generations per named lineage." })),
      h("nav", { "aria-label": "Model Registry views", class: "model-tabs" },
        h("button", { type: "button", class: "btn", "aria-pressed": String(active), onclick: () => { ui.tab = "registry"; ctx.rerender(); } }, "Registry"),
        h("button", { type: "button", class: "btn", "aria-pressed": String(!active), onclick: () => { ui.tab = "history"; ctx.rerender(); } }, "History"))),
    !editable ? h("p", { class: "model-reason", text: "This is your global machine registry; return to Machine scope to edit policy." }) : null,
    active ? h("div", {},
      h("div", { class: "model-toolbar" },
        h("input", { type: "search", class: "field", value: ui.query, "aria-label": "Search model registry",
          placeholder: "Find model, lineage, version or harness…", dataset: { testid: "model-search" },
          oninput: (ev) => { ui.query = ev.target.value; ctx.rerender(); } }),
        h("select", { class: "field", "aria-label": "Filter model harness", dataset: { testid: "model-harness" },
          onchange: (ev) => { ui.harness = ev.target.value; ctx.rerender(); } },
          h("option", { value: "", text: "All harnesses" }),
          harnesses.map((value) => h("option", { value, selected: ui.harness === value, text: value })))),
      h("div", { class: "model-layout" },
        h("div", { class: "model-table-wrap" },
          h("table", { class: "model-table", "aria-label": "Current model generation registry" },
            h("thead", {}, h("tr", {}, ...["Model", "Version", "Harness", "Efforts", "Installed", "My policy", "Details"]
              .map((s) => h("th", { scope: "col", text: s })))),
            h("tbody", {}, rows)),
          !matches.length ? h("p", { class: "muted", text: "No matching models; adjust search or harness." }) : null),
        h("aside", { class: "model-inspector-wrap", "aria-label": "Model details" },
          selected ? evidence(ctx, selected, data, { editable }) : h("p", { class: "muted", text: "Select a model to inspect." })),
        h("div", { class: "model-mobile-cards" }, cards)),
      editable ? confirmation(ctx, data, editable) : null,
      h("p", { class: "model-reason", text: "Catalog publication and rollback are not available in this foundation rollout. Unknown quota stays unknown. No background probe runs when opening Settings." }))
      : h("div", { class: "model-history", dataset: { testid: "model-history" } },
        h("p", { class: "sub", text: "Historical model records remain inspectable; none may be selected just because they exist here." }),
        (data.archive || []).length ?
          h("ul", {}, data.archive.map((e) => h("li", { text: (e.lineage || "Unclassified") + " " + (e.version || "") + " · " + e.model_id + " / " + e.harness + "@" + e.effort }))) :
          h("p", { class: "muted", text: "No displaced generations in the staged current catalog." }),
        (data.pending || []).length ? h("p", { class: "model-reason",
          text: String(data.pending.length) + " generations have unresolved release evidence and are held out of active routing." }) : null));
  return page;
}
