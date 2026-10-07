// A visible affordance for a region that scrolls sideways. Native scrollbars are overlay or hidden on many
// platforms, so a cut-off column looks like the end of the table. The hint appears only while columns are
// off-screen and its buttons scroll the region, so the hidden columns are one click away.
//
// The buttons use aria-disabled, never `disabled`, so the one the user just pressed keeps focus at the end of
// the scroll; `key` gives them stable data-keys so a rebuild of the surface (app.js `keep`) restores that focus.

const STEP = 0.8; // fraction of the visible width a click scrolls
// Whether each hint was showing at its last update. A rebuilt hint starts from that, not hidden: a hidden
// button cannot take back the focus `keep` restores, and the first measurement only arrives a frame later.
const showing = new Map();

export function scrollHint(h, scroller, noun, key) {
  const msg = h("span", { class: "msg" });
  const go = (dir, label) => h("button", {
    type: "button", class: "btn sm", "aria-label": `Scroll ${noun} ${label}`,
    dataset: { testid: `scroll-${label}`, key: `scroll:${key}:${label}` },
    onclick: (ev) => {
      if (ev.currentTarget.getAttribute("aria-disabled") !== "true") scroller.scrollBy({ left: dir * Math.max(120, scroller.clientWidth * STEP) });
    },
  }, dir < 0 ? "‹" : "›");
  const left = go(-1, "left");
  const right = go(1, "right");
  const hint = h("div", { class: "scroll-hint", role: "group", "aria-label": `Scroll ${noun}`, dataset: { testid: "scroll-hint" } },
    msg, left, right);
  const update = () => {
    const room = scroller.scrollWidth - scroller.clientWidth;
    const atStart = scroller.scrollLeft <= 1;
    const atEnd = scroller.scrollLeft >= room - 1;
    hint.hidden = room <= 1;
    showing.set(key, room > 1);
    left.setAttribute("aria-disabled", String(atStart));
    right.setAttribute("aria-disabled", String(atEnd));
    hint.dataset.more = room <= 1 ? "none" : atStart ? "right" : atEnd ? "left" : "both";
    msg.textContent = `More ${noun} off-screen: ${atEnd ? "scroll back" : "scroll sideways"}`;
  };
  hint.hidden = !showing.get(key);
  scroller.addEventListener("scroll", update, { passive: true });
  if (typeof ResizeObserver === "function") {
    // Updating in a frame keeps the hint's own height change out of the observation pass.
    const watch = new ResizeObserver(() => requestAnimationFrame(() => (hint.isConnected ? update() : watch.disconnect())));
    watch.observe(scroller);
    for (const child of scroller.children) watch.observe(child);
  } else {
    requestAnimationFrame(update);
  }
  return hint;
}
