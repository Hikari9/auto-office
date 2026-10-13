// Models & Routing: a single Office-authored registry projection. The browser
// may request a global user-policy mutation only through settings_set receipts;
// it never opens YAML, guesses live quota or grants adapter trust.
const ui = { query: "", harness: "", factual: "", policy: "", tab: "registry", selected: null, expanded: new Set(), confirm: null };
const title = (g) => g.model_id + " · " + g.version;
const routeSpec = (g) => g.harness + "/" + g.model_id;
const show = (v) => v === null || v === undefined ? "Unknown" : String(v);
const defined = (v) => v !== null && v !== undefined;
const caption = (e) => e.technical_state === "available" ? "Catalog route listed; live probe not run"
  : e.technical_state === "not-installed" ? "Harness not installed"
  : e.technical_state.replaceAll("-", " ");

// The row-level benchmark is one clearly named effort sample, not an average,
// rank, or permission/technical qualification. Inspect exposes every effort.
function sampleEffort(g) {
  const scored = (g.efforts || []).filter((e) => typeof e.benchmark_score === "number" && Number.isFinite(e.benchmark_score));
  return ["medium", "high", "low", "xhigh", "max"].map((effort) =>
    scored.find((e) => e.effort === effort)).find(Boolean) || scored[0] || null;
}

function factualStatus(g) {
  const efforts = g.efforts || [];
  if (!g.installed) return { key: "not-installed", label: "Harness not installed",
    detail: "Local harness absent; no live account/model probe performed" };
  const states = [...new Set(efforts.map((e) => e.technical_state || "unknown"))];
  if (!states.length) return { key: "unknown", label: "Technical status unknown", detail: "No effort evidence" };
  if (states.length > 1) return { key: "mixed", label: "Mixed effort states",
    detail: efforts.map((e) => e.effort + ": " + caption(e)).join(" · ") };
  const labels = {
    available: "Catalog listed — live unverified",
    "discovered-unconfirmed": "Discovery unconfirmed",
    "probe-pending": "Probe needed",
    "probe-passed": "Probe passed (not rechecked)",
    "temporarily-unavailable": "Temporarily unavailable",
    "confirmed-unsupported": "Unsupported effort",
    "not-installed": "Harness not installed",
  };
  const key = states[0];
  return { key, label: labels[key] || key.replaceAll("-", " "),
    detail: efforts.map((e) => e.effort + ": " + caption(e)).join(" · ") };
}

function policyStatus(g, data) {
  if (g.user_denied) return { key: "denied", label: "Denied by you" };
  const matchingRules = (data.policy?.overkill_rules || []).filter((rule) => rule && rule.route === routeSpec(g));
  if (matchingRules.some((rule) => Object.keys(rule).length === 1))
    return { key: "manual-only", label: "Manual-only by you" };
  // A role/size-specific overkill rule is not a blanket manual-only switch.
  if (matchingRules.length) return { key: "scoped-manual", label: "Scoped manual-only by you" };
  return { key: "allowed", label: "Allowed by you" };
}

function provenance(sample) {
  if (!sample) return { label: "Not sourced", identifier: "No scored effort", date: "Date unknown", url: null };
  const url = sample.source_metadata?.url;
  return { label: show(sample.source_name), identifier: show(sample.source_identifier),
    date: sample.source_metadata?.fetched_at ? "Fetched " + sample.source_metadata.fetched_at.slice(0, 10) : "Date unknown",
    url: typeof url === "string" && /^https:\/\/[^\s/]+/.test(url) ? url : null };
}

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
      g.legacy_overlay_denied ? h("p", { class: "model-reason", text: "Legacy overlay denial: use office model enable " + rule + " from the CLI, then re-read Settings." }) :
        (directDenied ? action("Remove direct denial", "allow") : action("Deny this model", "deny")),
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
  const factualOptions = [...new Map(all.map((g) => {
    const fact = factualStatus(g);
    return [fact.key, fact.label];
  })).entries()].sort((a, b) => a[1].localeCompare(b[1]));
  const matches = all.filter((g) => (!ui.harness || g.harness === ui.harness) &&
    (!ui.factual || factualStatus(g).key === ui.factual) &&
    (!ui.policy || policyStatus(g, data).key === ui.policy) &&
    (!q || [g.model_id, g.lineage, g.harness, g.version].some((v) => String(v || "").toLowerCase().includes(q))));
  // A temporary filter must not overwrite the user's earlier inspector choice.
  const selected = matches.find((g) => g.key === ui.selected) || matches[0] || null;
  const selectedKey = selected?.key || null;
  const active = ui.tab === "registry";
  const rowButton = (g) => h("button", {
    type: "button", class: "btn", "aria-pressed": String(g.key === selectedKey),
    onclick: () => { ui.selected = g.key; ui.confirm = null; ctx.rerender(); },
  }, "Inspect");
  const rows = matches.map((g) => {
    const sample = sampleEffort(g);
    const source = provenance(sample);
    const fact = factualStatus(g);
    return h("tr", { class: g.key === selectedKey ? "selected" : "",
      dataset: { testid: "model-registry-row", model: g.model_id } },
      h("th", { scope: "row", title: g.lineage },
        h("span", { class: "model-grid-main", text: g.model_id }),
        h("span", { class: "model-grid-sub", text: g.lineage })),
      h("td", { text: g.version }),
      h("td", {}, h("span", { class: "model-grid-main", text: g.harness }),
        h("span", { class: "model-grid-sub", title: sample?.invocation_model_id || "Invocation not verified",
          text: sample?.invocation_model_id ? sample.effort + ": " + sample.invocation_model_id : "Invocation not verified" })),
      h("td", { text: (g.efforts || []).map((e) => e.effort).join(", ") }),
      h("td", { dataset: { testid: "model-grid-benchmark" }, title: sample ?
        sample.effort + ": " + sample.benchmark_score + " · " + sample.benchmark_index : "No benchmark score" },
        h("span", { class: "model-grid-main", text: sample ? sample.benchmark_score + " · " + sample.effort : "Not scored" }),
        h("span", { class: "model-grid-sub", title: sample?.benchmark_index || data.index,
          text: sample?.benchmark_index || data.index || "Index unknown" })),
      h("td", { dataset: { testid: "model-grid-provenance" } },
        h("span", { class: "model-grid-main" }, source.url ?
          h("a", { href: source.url, target: "_blank", rel: "noopener noreferrer" }, source.label) : source.label),
        h("span", { class: "model-grid-sub", title: source.identifier, text: source.identifier }),
        h("span", { class: "model-grid-sub", text: source.date })),
      h("td", { dataset: { testid: "model-grid-factual" }, title: fact.detail },
        h("span", { class: "model-grid-main", text: fact.label }),
        h("span", { class: "model-grid-sub", text: "Per-effort status in Inspector" })),
      h("td", { text: g.installed ? "Detected" : "Not installed" }),
      h("td", { dataset: { testid: "model-grid-policy" }, text: policyStatus(g, data).label }),
      h("td", {}, rowButton(g)));
  });
  const cards = matches.map((g) => {
    const open = ui.expanded.has(g.key);
    const sample = sampleEffort(g);
    const source = provenance(sample);
    return h("article", { class: "model-card", dataset: { testid: "model-registry-card", model: g.model_id } },
      h("div", { class: "model-card-heading" },
        h("div", {}, h("h3", { text: title(g) }),
          h("p", { class: "sub", text: g.harness + " · " + (g.efforts || []).map((e) => e.effort).join(" / ") }),
          h("p", { class: "model-card-facts", text: factualStatus(g).label + " · " + policyStatus(g, data).label }),
          h("p", { class: "model-card-facts", text: sample ?
            "Benchmark " + sample.benchmark_score + " (" + sample.effort + ") · " + sample.benchmark_index : "Benchmark not scored" }),
          h("p", { class: "model-card-facts", text: "Source: " + source.label + " · " + source.identifier + " · " + source.date }),
          source.url ? h("a", { class: "model-card-source", href: source.url, target: "_blank",
            rel: "noopener noreferrer" }, "Open benchmark evidence") : null),
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
          placeholder: "Find model, lineage, version or harness…", dataset: { key: "model-search", testid: "model-search" },
          oninput: (ev) => { ui.query = ev.target.value; ctx.rerender(); } }),
        h("select", { class: "field", "aria-label": "Filter model harness", dataset: { key: "model-harness", testid: "model-harness" },
          onchange: (ev) => { ui.harness = ev.target.value; ctx.rerender(); } },
          h("option", { value: "", text: "All harnesses" }),
          harnesses.map((value) => h("option", { value, selected: ui.harness === value, text: value }))),
        h("select", { class: "field", "aria-label": "Filter factual availability", dataset: { key: "model-factual", testid: "model-factual-filter" },
          onchange: (ev) => { ui.factual = ev.target.value; ctx.rerender(); } },
          h("option", { value: "", text: "All factual states" }),
          factualOptions.map(([value, label]) => h("option", { value, selected: ui.factual === value, text: label }))),
        h("select", { class: "field", "aria-label": "Filter my model policy", dataset: { key: "model-policy", testid: "model-policy-filter" },
          onchange: (ev) => { ui.policy = ev.target.value; ctx.rerender(); } },
          h("option", { value: "", text: "All policy states" }),
          [["allowed", "Allowed by you"], ["manual-only", "Manual-only by you"],
            ["scoped-manual", "Scoped manual-only by you"], ["denied", "Denied by you"]].map(
            ([value, label]) => h("option", { value, selected: ui.policy === value, text: label })))),
      !matches.length ? h("p", { class: "model-empty", role: "status", "aria-live": "polite",
        dataset: { testid: "model-empty" }, text: "No matching models; adjust search, harness or status filters." }) : null,
      h("div", { class: "model-layout" },
        h("div", { class: "model-table-wrap", dataset: { scrollKey: "model-grid" } },
          h("table", { class: "model-table", "aria-label": "Current model generation registry" },
            h("thead", {}, h("tr", {}, ...["Model", "Version", "Harness / invocation", "Efforts", "Benchmark sample", "Provenance", "Factual availability", "Installed", "My policy", "Details"]
              .map((label) => h("th", { scope: "col", text: label })))),
            h("tbody", {}, rows))),
        h("aside", { class: "model-inspector-wrap", "aria-label": "Model details", dataset: { scrollKey: "model-inspector-scroll" } },
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
