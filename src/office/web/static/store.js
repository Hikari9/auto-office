// Live Office store: the SSE snapshot plus deltas, reconnecting with backoff.
//
// A delta applies only when its epoch matches and its base_rev equals the
// local rev. A rev at or below the local rev is a duplicate and is ignored.
// Any gap, another epoch or a `resync` event replaces the whole state. The
// store never decides anything itself: it only mirrors the service.

const COLLECTIONS = ["repos", "issues", "prs", "runs", "tasks", "agents", "queue", "commands"];
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000];

export function applyDelta(state, d) {
  if (d.epoch !== state.epoch) return "resync";
  if (d.rev <= state.rev) return "duplicate";
  if (d.base_rev !== state.rev) return "resync";
  for (const [coll, items] of Object.entries(d.upserts || {})) {
    state.entities[coll] = Object.assign(state.entities[coll] || {}, items);
  }
  for (const [coll, ids] of Object.entries(d.removes || {})) {
    for (const id of ids) delete (state.entities[coll] || {})[id];
  }
  for (const [key, value] of Object.entries(d.scalars || {})) {
    if (key === "freshness") state.freshness = value;
    else state.scalars[key] = value;
  }
  state.rev = d.rev;
  return "applied";
}

export class Store {
  constructor({ url = "/api/stream", EventSourceImpl = globalThis.EventSource } = {}) {
    this.url = url;
    this.EventSourceImpl = EventSourceImpl;
    this.state = null;
    this.status = "connecting"; // connecting | live | reconnecting | disconnected
    this.attempt = 0;
    this.listeners = new Set();
    this.es = null;
    this.timer = null;
    this.stats = { applied: 0, duplicates: 0, resyncs: 0, snapshots: 0 };
  }

  subscribe(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }

  emit(reason) { for (const fn of this.listeners) fn(this, reason); }

  connect({ fresh = false } = {}) {
    clearTimeout(this.timer);
    if (this.es) this.es.close();
    const resume = !fresh && this.state ? `?last_event_id=${encodeURIComponent(`${this.state.epoch}:${this.state.rev}`)}` : "";
    const es = new this.EventSourceImpl(this.url + resume);
    this.es = es;
    const parsed = (fn) => (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { this.connect({ fresh: true }); return; }
      fn(data, ev.type);
    };
    es.addEventListener("snapshot", parsed((d, type) => this.replace(d, type)));
    es.addEventListener("resync", parsed((d, type) => this.replace(d, type)));
    es.addEventListener("delta", parsed((d) => this.delta(d)));
    es.addEventListener("open", () => {
      this.attempt = 0; // a stream that came back resets the backoff
      if (this.state) this.setStatus("live");
    });
    es.onerror = () => {
      if (this.es !== es) return;
      es.close();
      this.es = null;
      this.setStatus(this.attempt >= 2 ? "disconnected" : "reconnecting");
      const wait = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)];
      this.attempt += 1;
      this.timer = setTimeout(() => this.connect(), wait);
    };
  }

  setStatus(status) {
    if (this.status === status) return;
    this.status = status;
    this.emit("status");
  }

  replace(snapshot, kind) {
    if (kind === "resync") this.stats.resyncs += 1; else this.stats.snapshots += 1;
    for (const coll of COLLECTIONS) snapshot.entities[coll] = snapshot.entities[coll] || {};
    snapshot.scalars = snapshot.scalars || {};
    this.state = snapshot;
    this.attempt = 0;
    this.status = "live";
    this.emit(kind);
  }

  delta(d) {
    if (!this.state) return "ignored";
    const outcome = applyDelta(this.state, d);
    if (outcome === "duplicate") { this.stats.duplicates += 1; return outcome; }
    if (outcome === "resync") { this.stats.resyncs += 1; this.connect({ fresh: true }); return outcome; }
    this.stats.applied += 1;
    this.status = "live";
    this.emit("delta");
    return outcome;
  }
}
