// Routing inspection for one task: the decision Office recorded, never recomputed.
// A compact summary plus an evidence drawer; runs without route_audit rows say so.

const NONE = "no routing audit recorded";

export function routingView(h, route, history = []) {
  if (!route || !route.available) {
    return h("section", { class: "routing legacy", dataset: { testid: "routing" } },
      h("h3", { text: "Routing" }),
      h("p", { class: "muted", dataset: { testid: "routing-legacy" }, text: NONE }),
      route && route.dispatched ? h("p", { text: `Dispatched as ${route.dispatched}` }) : null);
  }
  const taken = route.fallbacks_taken || [];
  const row = (label, value, testid) => h("div", { class: "kv" }, h("dt", { text: label }),
    h("dd", { dataset: { testid }, text: value === null || value === undefined || value === "" ? "not recorded" : value }));
  return h("section", { class: "routing", dataset: { testid: "routing" } },
    h("h3", { text: "Routing" }),
    h("dl", { class: "kvs compact" },
      row("Primary", route.primary, "route-primary"),
      row("Fallbacks", (route.fallbacks || []).join(", ") || "none", "route-fallbacks"),
      row("Reason", route.reason, "route-reason"),
      row("Strength", route.strength, "route-strength"),
      row("Weakness", route.weakness, "route-weakness"),
      row("Dispatched", route.dispatched, "route-dispatched"),
      row("Fallback taken", taken.length ? taken.map((f) => `${f.route} (${f.reason || "no reason recorded"})`).join("; ")
        : "none", "route-fallback-taken")),
    h("details", { class: "evidence", dataset: { testid: "routing-evidence" } },
      h("summary", { text: "Evidence" }),
      h("p", { class: "muted", dataset: { testid: "route-provenance" },
        text: `Provenance: runs.db route_audit (${(route.audits || []).length} recorded decision${(route.audits || []).length === 1 ? "" : "s"})` }),
      h("p", { dataset: { testid: "route-override" }, text: route.planner_override
        ? `Planner override: ${route.planner_override.why || "no reason recorded"}${route.planner_override.departs_from_ranking ? " (departs from ranking)" : ""}`
        : "Planner override: none" }),
      h("table", { class: "audits", dataset: { testid: "route-audits" } },
        h("thead", {}, h("tr", {}, ...["Audit", "Phase", "Plan", "Primary", "Dispatched", "Explored", "Decision", "At"]
          .map((c) => h("th", { text: c })))),
        h("tbody", {}, ...(route.audits || []).map((a) => h("tr", {},
          ...[a.id, a.phase, a.plan_version, a.primary_route, a.dispatched_route || "-", a.explored ? "yes" : "no",
            a.decision_hash, a.created_at].map((v) => h("td", { text: v === null || v === undefined ? "-" : String(v) })))))),
      h("h4", { text: "Route history" }),
      history.length
        ? h("ol", { class: "route-history", dataset: { testid: "route-history" } }, ...history.map((d) =>
          h("li", { text: `${d.started_at || "?"} ${d.harness || "?"}/${d.model || "?"}@${d.effort || "?"} · ${d.status || "current"}` })))
        : h("p", { class: "muted", dataset: { testid: "route-history" }, text: "No earlier dispatches for this task" })));
}
