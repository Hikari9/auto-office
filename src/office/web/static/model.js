// View model: Office/GitHub entities -> repository and issue rows, and the
// action states the server's records allow. Nothing here is guessed: a value
// the service did not send is shown as unavailable.

export const AUTHORIZATIONS = [
  { value: "preview", label: "Preview only" },
  { value: "ask", label: "PR / no merge" },
  { value: "merge", label: "Merge to main" },
  { value: "e2e", label: "Production" },
];
// The browser has no plan-authorization path: the user runs this with their own words.
export const PLAN_APPROVAL_COMMAND = 'office approve plan --quote "<words>"';
export const QUEUE_AUTHORIZATION = "preview";
export const QUEUE_ONLY_WHY = "Queued work launches with the repository default (Preview only); a per-issue override is not supported by this service";
const END_STATE_LABELS = { preview: "Preview only", ask: "PR / no merge", merge: "Merge to main", e2e: "Production",
  merged: "Merged", pr: "PR" };
const LIVENESS_ORDER = { live: 0, resumable: 1, none: 2, terminal: 3 };

export const repoKey = (fullName) => `repo:github.com/${String(fullName).toLowerCase()}`;
export const shortRun = (id) => String(id || "").replace(/^run:/, "").slice(0, 8);
export const endStateLabel = (v) => (v ? END_STATE_LABELS[v] || v : null);

function ghSourceState(sources) {
  const states = Object.values(sources || {}).map((s) => s && s.state).filter(Boolean);
  return ["unauthenticated", "revoked", "rate_limited", "stale", "disabled"].find((s) => states.includes(s))
    || (states.length ? "fresh" : null);
}

// GitHub status of one repository: [tone, label, detail].
export function githubMark(repo, freshness) {
  const gh = repo.github;
  const sources = ((freshness.github || {}).sources || {}).repos || {};
  const src = gh ? sources[gh.full_name] : null;
  if (!gh) return ["off", "GitHub n/a", "not visible to the GitHub token"];
  if (gh.access === "revoked") return ["err", "GitHub revoked", "access revoked"];
  if (gh.archived) return ["off", "Archived", "repository is archived"];
  const state = ghSourceState(src);
  if (state === "rate_limited") return ["warn", "Rate limited", "GitHub rate limit"];
  if (state === "stale") return ["warn", "GitHub stale", "GitHub data is stale"];
  if (gh.has_issues === false) return ["off", "Issues off", "issues are disabled"];
  return ["ok", "GitHub", "visible"];
}

// Local status of one repository: [tone, label, detail].
export function localMark(repo) {
  const r = repo.readiness;
  if (!r) return repo.runs && repo.runs.length ? ["ok", "Local runs", "known from runs.db only"] : ["off", "Local n/a", "no readiness"];
  if (r.ready) return ["ok", "Ready", "execution-ready"];
  if (!r.checkout) return ["err", "No checkout", "local repository unavailable"];
  return ["warn", "Not ready", `failing: ${(r.failing || []).join(", ")}`];
}

export function repoName(repo) {
  return repo.full_name || (repo.github && repo.github.full_name) || repo.id;
}

function prsFor(runs, prs) {
  const out = [];
  for (const run of runs) {
    for (const p of run.prs || []) {
      const live = (p.ref && prs[p.ref]) || {};
      out.push({ ...p, run: run.id, github: live.github || null, github_checks: live.github_checks ?? null });
    }
  }
  return out;
}

function gatesText(run) {
  const office = run && run.gates && run.gates.office;
  if (!office) return null;
  if (!office.total) return "none recorded";
  return Object.entries(office.by_kind).map(([kind, k]) => {
    const verdicts = Object.entries(k.verdict).map(([v, n]) => `${n} ${v}`).join(", ");
    return `${kind.replace(/_/g, " ")} ${verdicts || Object.entries(k.status).map(([s, n]) => `${n} ${s}`).join(", ")}`;
  }).join("; ");
}

function checksText(prs) {
  const states = prs.map((p) => p.github_checks).filter(Boolean);
  if (!prs.length) return null;
  if (!states.length) return "unavailable";
  const counts = {};
  for (const s of states) counts[s] = (counts[s] || 0) + 1;
  return Object.entries(counts).map(([s, n]) => `${n} ${s}`).join(", ");
}

// Every issue row: GitHub's open issues plus issues known only from an Office run record.
// The task line under a run's phase: its unsettled tasks and their states (e.g. `T7 submitted · T9 running`).
const SETTLED = new Set(["accepted", "cancelled"]);
const TASKS_SHOWN = 3;
function phaseDetail(run, tasks) {
  if (!run) return "No run yet";
  const open = (tasks || []).filter((t) => !SETTLED.has(t.status));
  if (!open.length) {
    if (run.awaiting_plan_authorization) return "Awaiting plan authorization";
    if (!tasks || !tasks.length) return run.liveness === "terminal" ? "Run closed" : "No tasks recorded";
    return `All ${tasks.length} tasks settled`;
  }
  const shown = open.slice(0, TASKS_SHOWN).map((t) => `${t.task_id} ${String(t.status || "unknown").replace(/_/g, " ")}`);
  return shown.join(" · ") + (open.length > TASKS_SHOWN ? ` · +${open.length - TASKS_SHOWN} more` : "");
}

const ATTENTION_TASK = new Set(["paused", "failed", "blocked", "needs_attention", "stopped"]);
function needsAttention(run, tasks) {
  if (!run || run.liveness === "terminal") return false;
  return run.liveness === "resumable" || Boolean(run.awaiting_plan_authorization)
    || (tasks || []).some((t) => ATTENTION_TASK.has(t.status));
}

// The Issues filter chips: which rows each shows.
export const FILTERS = [
  { value: "open", label: "Open + running", test: (r) => r.liveness !== "terminal" },
  { value: "incoming", label: "Incoming", test: (r) => !r.run },
  { value: "attention", label: "Needs attention", test: (r) => r.attention },
  { value: "done", label: "Done", test: (r) => r.liveness === "terminal" },
];

// `owner/name#n` in lower case for any issue ref `office queue add` accepts (`o/r#5` or an issue URL).
export function canonicalRef(ref) {
  const m = /([\w.-]+\/[\w.-]+?)(?:#|\/issues\/)(\d+)$/.exec(String(ref || "").trim());
  return m ? `${m[1].toLowerCase()}#${Number(m[2])}` : null;
}

// Issue-kind queue entries by their canonical ref.
function queuedIssues(queue) {
  const out = new Map();
  for (const e of Object.values(queue)) {
    const ref = e.kind === "issue" && canonicalRef(e.ref);
    if (ref) out.set(ref, e);
  }
  return out;
}

export function issueRows(state) {
  const { issues = {}, runs = {}, repos = {}, prs = {}, queue = {}, tasks = {} } = state.entities;
  const tasksByRun = new Map();
  for (const t of Object.values(tasks)) {
    if (!tasksByRun.has(t.run)) tasksByRun.set(t.run, []);
    tasksByRun.get(t.run).push(t);
  }
  const issueQueue = queuedIssues(queue);
  const rows = new Map();
  const add = (id, base) => {
    if (!rows.has(id)) rows.set(id, base);
    return rows.get(id);
  };
  for (const i of Object.values(issues)) {
    add(i.id, { id: i.id, repoKey: repoKey(i.repo), repoName: i.repo, number: i.number, title: i.title, url: i.url,
      provenance: "github", runIds: [...(i.runs || [])], liveRun: i.live_run, resumableRun: i.resumable_run });
  }
  for (const r of Object.values(runs)) {
    const ref = r.issue && r.issue.ref;
    if (!ref || rows.has(ref)) continue;
    const key = (r.repo && r.repo.key) || r.issue.repo;
    const repo = repos[key] || {};
    add(ref, { id: ref, repoKey: key, repoName: repoName(repo) || (r.repo && r.repo.slug) || key, number: r.issue.number ?? 0,
      title: null, url: r.issue.url, provenance: "office-record", runIds: [], liveRun: null, resumableRun: null });
  }
  for (const r of Object.values(runs)) {
    const row = r.issue && rows.get(r.issue.ref);
    if (!row || row.provenance !== "office-record") continue;
    row.runIds.push(r.id);
    if (r.liveness === "live" && !row.liveRun) row.liveRun = r.id;
    if (r.liveness === "resumable" && !row.resumableRun) row.resumableRun = r.id;
  }
  const out = [];
  for (const row of rows.values()) {
    const linked = row.runIds.map((id) => runs[id]).filter(Boolean);
    const primary = runs[row.liveRun] || runs[row.resumableRun] || linked[linked.length - 1] || null;
    const linkedPrs = prsFor(linked, prs);
    const q = primary ? queue[primary.id] : null;
    const repo = repos[row.repoKey] || null;
    const runTasks = primary ? (tasksByRun.get(primary.id) || []).sort((a, b) => String(a.task_id).localeCompare(String(b.task_id), undefined, { numeric: true })) : null;
    out.push({
      ...row, repo, runs: linked, run: primary, prs: linkedPrs, queue: q,
      liveness: primary ? primary.liveness : "none",
      phase: primary ? primary.phase : "Incoming",
      phaseDetail: phaseDetail(primary, runTasks),
      attention: needsAttention(primary, runTasks),
      queued: primary ? null : issueQueue.get(canonicalRef(`${row.repoName}#${row.number}`)) || null,
      owner: primary && primary.owner && primary.owner.kind === "session" ? `${primary.owner.harness} orchestrator` : "No owner",
      progress: primary ? primary.progress : null,
      priority: q ? q.priority : null,
      authorization: primary ? endStateLabel(primary.end_state && primary.end_state.value) : null,
      gates: gatesText(primary),
      checks: checksText(linkedPrs),
      search: `${row.repoName} #${row.number} ${row.title || ""} ${linked.map((r) => r.run_id).join(" ")}`.toLowerCase(),
    });
  }
  out.sort((a, b) => (LIVENESS_ORDER[a.liveness] ?? 4) - (LIVENESS_ORDER[b.liveness] ?? 4)
    || a.repoName.localeCompare(b.repoName) || a.number - b.number);
  return out;
}

export function startCommand(url, endState) {
  const q = (s) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\"'\"'")}'`);
  return `office start --issue ${q(url)} --end-state ${q(endState)} "Resolve issue ${url}"`;
}

const off = (reason) => ({ enabled: false, reason });
const on = () => ({ enabled: true, reason: null });

function control(run, kind) {
  const c = run && run.controls && run.controls[kind];
  if (!c) return off("this run reports no web controls");
  return c.allowed ? on() : off(c.reason || "not allowed for this run");
}

// The actions one issue row offers, each enabled or disabled with the server's reason.
export function issueActions(row, state, inFlight = () => false) {
  const office = (state.freshness && state.freshness.office) || {};
  const stale = office.state !== "live" ? `Office data is ${office.state || "unavailable"}; commands wait until it is live` : null;
  const gate = (a) => (stale && a.enabled ? off(stale) : a);
  const out = { primary: null, list: [] };
  const live = state.entities.runs[row.liveRun];
  const resumable = state.entities.runs[row.resumableRun];
  if (live) {
    out.list.push({ kind: "attach_run", label: "Attach", run: live, ...gate(control(live, "attach_run")) });
  } else if (resumable) {
    out.list.push({ kind: "resume_run", label: "Resume", run: resumable, ...gate(control(resumable, "resume_run")) });
    out.list.push({ kind: "attach_run", label: "Attach", run: resumable, ...gate(control(resumable, "attach_run")) });
  } else {
    out.list.push({ kind: "copy_start", label: "Copy office start", enabled: Boolean(row.url),
      reason: row.url ? null : "the issue has no URL" });
    const repo = row.repo;
    const gh = repo && repo.github;
    const readiness = repo && repo.readiness;
    let why = null;
    if (!gh || gh.access === "revoked") why = "GitHub access to this repository is unavailable";
    else if (!readiness) why = "repository readiness is unknown";
    else if (!readiness.ready) why = `repository not execution-ready: ${(readiness.failing || []).join(", ")}`;
    else if (!gh.full_name) why = "the repository's owner/name is unknown";
    const launcher = state.scalars.launcher || {};
    const startWhy = why || (launcher.available ? null : launcher.reason || "no launcher capability");
    out.list.push({ kind: "start_issue", label: "Start", ...gate(startWhy ? off(startWhy) : on()) });
    const queued = row.queued ? off(`already in the Auto Queue (${row.queued.decision || "waiting"})`) : null;
    out.list.push({ kind: "queue_issue", label: "Auto Queue", ...gate(why ? off(why) : queued || on()) });
  }
  for (const a of out.list) {
    if (a.enabled && a.kind !== "copy_start" && inFlight(a.kind, a.run ? a.run.id : row.id)) {
      Object.assign(a, off("the same command is still in flight or has an unknown result; check it before sending again"));
    }
  }
  out.primary = out.list[0];
  return out;
}

// Receipt display state: pending covers a request in flight and accepted/running receipts.
export function receiptState(status) {
  return status === "accepted" || status === "running" || status === "pending" ? "pending" : status;
}
