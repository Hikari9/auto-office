// Orchestrator chat: a composer bound to one exact target {host, repo, run, session}.
// Drafts and receipts are kept per target key, so changing the selection never
// moves a draft. A send is refused unless the selected target still equals the
// draft's target. An unknown delivery is never resent silently: "Send again"
// needs an explicit confirmation and creates a new command id (resend_of).

const drafts = new Map(); // target key -> text
const sent = new Map(); // target key -> [{id, text}]
const confirming = new Set(); // command ids awaiting a "Send again" confirmation

export const targetKey = (t) => [t.host, t.repo, t.run_id, t.session].join("|");

// The chat target of one orchestrator node, or null when it is not an orchestrator.
export function chatTarget(state, node) {
  if (!node || node.kind !== "session" || node.column !== "orchestrators") return null;
  const run = state.entities.runs[node.run] || {};
  return { host: ((state.scalars || {}).host || {}).id || null, repo: (run.repo && run.repo.slug) || (run.repo && run.repo.key) || null,
    run_id: run.run_id || null, session: node.id };
}

// Why the composer is disabled for this target, or null when it may send.
export function chatBlocked(state, status, node) {
  const office = (state.freshness && state.freshness.office) || {};
  if (status === "disconnected" || status === "reconnecting") return `The live stream is ${status}; chat waits until it is live`;
  if (office.state !== "live") return `Office data is ${office.state || "unavailable"}; chat waits until it is live`;
  const run = state.entities.runs[node.run];
  if (!run) return "The run is no longer in Office's data";
  const cap = run.controls && run.controls.chat_send;
  if (!cap || !cap.allowed) return `This orchestrator is not chat-capable: ${(cap && cap.reason) || "no chat control reported"}`;
  const s = node.state || {};
  if (s.stale) return "The orchestrator session is stale";
  if (s.unavailable) return "The orchestrator session is unavailable";
  const t = chatTarget(state, node);
  if (!t.host || !t.run_id) return "The chat target is not fully known (host or run missing)";
  return null;
}

const RECEIPT_TEXT = { pending: "pending", delivered: "delivered", failed: "failed", unknown: "unknown" };

function deliveryState(status) {
  if (status === "completed") return "delivered";
  if (status === "failed") return "failed";
  if (status === "unknown" || status === "checked") return "unknown";
  return "pending";
}

export function chatComposer(ctx, node) {
  const { h, store } = ctx;
  const state = store.state;
  const target = chatTarget(state, node);
  const key = targetKey(target);
  const blocked = chatBlocked(state, store.status, node);
  const text = drafts.get(key) || "";

  const submit = (resendOf = null, body = null) => {
    const current = ctx.selectedNode();
    const now = current && chatTarget(store.state, current);
    if (!now || targetKey(now) !== key) return; // selection moved: never retarget
    if (chatBlocked(store.state, store.status, current)) return;
    const message = body ?? (drafts.get(key) || "");
    if (!message.trim()) return;
    const id = ctx.newId();
    const list = sent.get(key) || [];
    list.push({ id, text: message });
    sent.set(key, list);
    if (resendOf === null) drafts.delete(key);
    ctx.send("chat_send", node.id, { host: target.host, run_id: target.run_id, session: target.session },
      { text: message, ...(resendOf ? { resend_of: resendOf } : {}) }, {}, `Chat ${target.run_id}`, id);
  };

  const receipts = (sent.get(key) || []).map((m) => {
    const r = ctx.receipt(m.id);
    const st = deliveryState(r.status);
    const again = st === "unknown"
      ? (confirming.has(m.id)
        ? h("span", { class: "confirm" },
          h("span", { text: "Send this message again as a new command?" }),
          h("button", { type: "button", class: "btn primary", dataset: { testid: "chat-resend-confirm", key: `resend-ok:${m.id}` },
            disabled: Boolean(blocked), onclick: () => { confirming.delete(m.id); submit(m.id, m.text); } }, "Confirm send again"),
          h("button", { type: "button", class: "btn", dataset: { testid: "chat-resend-cancel", key: `resend-no:${m.id}` },
            onclick: () => { confirming.delete(m.id); ctx.rerender(); } }, "Cancel"))
        : h("button", { type: "button", class: "btn", dataset: { testid: "chat-resend", key: `resend:${m.id}` },
          onclick: () => { confirming.add(m.id); ctx.rerender(); } }, "Send again"))
      : null;
    return h("li", { class: "chat-receipt", dataset: { testid: "chat-receipt", status: st, id: m.id }, title: r.error || m.id },
      h("span", { class: "msg", text: m.text }), h("span", { class: "rstate", text: RECEIPT_TEXT[st] }), again);
  });

  const section = h("section", { class: "chat", dataset: { testid: "chat" }, "aria-label": "Orchestrator chat" },
    h("h3", { text: "Chat with orchestrator" }),
    h("dl", { class: "kvs chat-target", dataset: { testid: "chat-target", key: key } },
      ...[["Host", target.host], ["Repository", target.repo], ["Run", target.run_id], ["Session", target.session]]
        .map(([k, v]) => h("div", { class: "kv" }, h("dt", { text: k }), h("dd", { text: v || "unknown" })))),
    receipts.length ? h("ol", { class: "chat-receipts" }, receipts) : null,
    h("textarea", { class: "field", rows: "3", dataset: { testid: "chat-input", key: `chat:${key}` },
      "aria-label": `Message to ${target.session}`, disabled: Boolean(blocked),
      oninput: (ev) => { drafts.set(key, ev.target.value); },
      onkeydown: (ev) => { if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); submit(); } } }),
    h("div", { class: "chat-actions" },
      h("button", { type: "button", class: "btn primary", dataset: { testid: "chat-send", key: `send:${key}` },
        disabled: Boolean(blocked), onclick: () => submit() }, "Send"),
      blocked ? h("span", { class: "why", dataset: { testid: "chat-blocked" }, text: blocked }) : null));
  section.querySelector("textarea").value = text;
  return section;
}
