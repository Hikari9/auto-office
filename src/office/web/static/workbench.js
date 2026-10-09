// Auto Office run-first workbench. A non-authoritative view over the existing
// Office SSE projection; all semantic actions still go through /api/commands.
// Opt-in until maintainer visual sign-off (#442): ?workbench=1 or the stored
// preference (Settings). The original Workstation is the default and ?classic=1.
import { chatBlocked, chatTarget, targetKey } from './chat.js';
import { issueRows, repoName, shortRun, PLAN_APPROVAL_COMMAND } from './model.js';

const store = window.officeStore;
const params = new URLSearchParams(location.search);
const fixture = document.querySelector('meta[name="office-fixture"]')?.content || '';
const token = document.querySelector('meta[name="office-token"]')?.content || '';
const MODE_KEY = 'office-workbench-mode';
const RUN_KEY = 'office-workbench-run';
const UI_KEY = 'office-workbench-ui';
const store_ = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch {} },
};
// Reversible flag: ?workbench=0 clears the stored preference, ?workbench=1 opts in for this load,
// ?classic=1 always wins. Without either, the stored preference decides (default: classic).
function workbenchEnabled() {
  if (params.has('classic')) return false;
  if (params.get('workbench') === '0') { store_.set(MODE_KEY, null); return false; }
  if (params.has('workbench')) return true;
  return store_.get(MODE_KEY) === 'workbench';
}
function loadUi() { try { return JSON.parse(store_.get(UI_KEY) || '{}') || {}; } catch { return {}; } }
function saveUi() {
  store_.set(UI_KEY, JSON.stringify({ query: state.query, issueQuery: state.issueQuery,
    sidebarMini: $('ww')?.classList.contains('ww-sidebar-mini') || false, inspector: state.showInspector }));
}
const $ = (id) => document.getElementById(id);
const state = {
  runId: null, view: 'run', query: '', expanded: new Set(), inspector: 'overview',
  showInspector: !matchMedia('(max-width:1100px)').matches, showSidebar: false, palette: false, activity: new Map(),
  drafts: new Map(), localSends: new Map(), pending: new Set(), updatePending: false,
  activitySequence: 0, runScroll: new Map(), activeAgent: null, issueQuery: '', wish: null, notFound: null,
};
const ICON = {
  search: '<circle cx="11" cy="11" r="7"/><path d="m16 16 4 4"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3z"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
  terminal: '<path d="m4 7 5 5-5 5M12 17h8"/>',
  git: '<circle cx="6" cy="4" r="2"/><circle cx="6" cy="20" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 6v12M8 5h6a4 4 0 0 1 4 4"/>',
  inbox: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 13h5l2 3h4l2-3h5"/>',
  agents: '<circle cx="8" cy="8" r="3"/><circle cx="18" cy="9" r="2"/><path d="M2 20a6 6 0 0 1 12 0M14 16a5 5 0 0 1 8 4"/>',
  chart: '<path d="M4 20V4m0 16h17M9 17v-6M14 17V7M19 17v-9"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M4.9 4.9l2.1 2.1m10 10 2.1 2.1m0-14.2L17 7m-10 10-2.1 2.1"/>',
  send: '<path d="m3 3 18 9-18 9 3-9zM6 12h15"/>',
  external: '<path d="M14 4h6v6m0-6-9 9"/><path d="M20 13v6H4V4h7"/>',
  alert: '<path d="M12 3 2 21h20L12 3zM12 10v4m0 3h.01"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/>',
};
function icon(name) { return `<svg class="ww-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name] || ICON.clock}</svg>`; }
function el(tag, attrs = {}, ...contents) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, String(value));
  }
  for (const c of contents.flat(Infinity)) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}
const text = (str, cls = '') => el('span', {class: cls, text: str ?? 'Unavailable'});
const button = (label, handler, cls = '', opts = {}) => el('button', {type:'button', class:`ww-button ${cls}`, onclick:handler, ...opts}, label);
const simple = (name) => {const s = el('span',{class:'ww-iconbox'});s.innerHTML=icon(name);return s;};
const safeUrl = (url) => { try { const u = new URL(url); return u.protocol === 'https:' ? u.href : null; } catch { return null; } };
function external(label, url) { const href = safeUrl(url); return href ? el('a', {href, target:'_blank',rel:'noopener noreferrer',class:'ww-link'}, label) : text(label,'ww-muted'); }
const allRuns = () => Object.values(store?.state?.entities?.runs || {}).filter(r => r && r.id && r.run_id);
const repoLabel = (run) => run.repo?.slug || store?.state?.entities?.repos?.[run.repo?.key]?.full_name || run.repo?.local_key || run.repo?.key || 'Unknown repository';
const issueLabel = (run) => run.issue?.number ? `#${run.issue.number}` : 'No linked issue';
function titleFor(run) {
  const issue = run.issue?.ref && store.state?.entities?.issues?.[run.issue.ref];
  return issue?.title || run.goal || `Run ${shortRun(run.run_id)}`;
}
function statusFor(run) {
  const tasks = run.tasks || [];
  if (run.liveness === 'terminal') return [run.phase === 'abandoned' ? 'Abandoned' : 'Closed', run.phase === 'abandoned' ? 'warn' : 'quiet'];
  if (run.awaiting_plan_authorization || tasks.some(t => ['blocked','failed','needs_attention','paused'].includes(t.status))) return ['Needs input', 'warn'];
  if (run.liveness === 'resumable') return ['Resumable', 'quiet'];
  return run.liveness === 'live' ? ['Live', 'live'] : ['Unknown', 'quiet'];
}
const badge = (label, tone='quiet') => el('span', {class:`ww-badge ${tone}`},el('span',{class:'ww-dot'}),text(label));
const when = (value) => {
  if (!value) return 'Time unavailable';
  const n = Date.parse(value);
  if (!Number.isFinite(n)) return 'Time unavailable';
  const seconds = Math.max(0, Math.floor((Date.now()-n)/1000));
  if (seconds < 60) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds/60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds/3600)}h ago`;
  return `${Math.floor(seconds/86400)}d ago`;
};
const sortRuns = (runs) => [...runs].sort((a,b) => {
  const rank = (r) => { const st=statusFor(r)[1]; return st==='warn'?0:st==='live'?1:st==='quiet'&&r.liveness==='resumable'?2:3; };
  return rank(a)-rank(b) || (Date.parse(b.updated_at||b.created_at||0)||0)-(Date.parse(a.updated_at||a.created_at||0)||0) || a.id.localeCompare(b.id);
});
const selected = () => store?.state?.entities?.runs?.[state.runId] || null;
// Deep link: #repo=<repo key>&run=<record id>. Other hash parameters are preserved.
const hashParams = () => new URLSearchParams(location.hash.slice(1));
function wishFromHash() {
  const frag = hashParams(), run = frag.get('run');
  return run ? { run, repo: frag.get('repo') || null } : null;
}
const findWish = (wish) => allRuns().find(r => r.id === wish.run && (!wish.repo || (r.repo?.key || repoLabel(r)) === wish.repo));
function expandFor(run) { if (run) state.expanded.add(run.repo?.key || repoLabel(run)); }
function chooseRun() {
  const runs = allRuns();
  if (state.wish && store.state) {
    const hit = findWish(state.wish);
    if (hit) { state.runId = hit.id; state.notFound = null; }
    else { state.runId = null; state.notFound = state.wish; }
    state.wish = null;
  }
  if (state.notFound && !state.runId) return;
  if (state.runId && runs.some(r => r.id === state.runId)) { expandFor(selected()); return; }
  const last = store_.get(RUN_KEY);
  state.runId = runs.find(r => r.id === last)?.id || sortRuns(runs)[0]?.id || null;
  const run = selected(); expandFor(run);
  // Stamp the initial run on the current entry so Back returns to it rather than to a bare URL.
  if (run && !wishFromHash()) history.replaceState(null, '', linkFor(run).slice(location.origin.length));
}
function linkFor(run) {
  const frag = hashParams();
  frag.set('repo', run.repo?.key || repoLabel(run)); frag.set('run', run.id);
  return `${location.origin}${location.pathname}${location.search}#${frag.toString()}`;
}
function selectRun(id, {push = true} = {}) {
  if (!store.state?.entities?.runs?.[id]) return;
  if (state.view === 'run') state.runScroll.set(state.runId, $('ww-feed')?.scrollTop || 0);
  state.runId=id; state.view='run'; state.inspector='overview'; state.showSidebar=false; state.notFound=null;
  const run = selected(); expandFor(run);
  store_.set(RUN_KEY, id);
  if (push && run) { const next = linkFor(run); if (next !== location.href) history.pushState(null, '', next.slice(location.origin.length)); }
  draw(); fetchActivity(true);
}
function applyHash() {
  const wish = wishFromHash(); if (!wish) return;
  const hit = store.state && findWish(wish);
  if (hit) { if (hit.id !== state.runId || state.view !== 'run') selectRun(hit.id, {push: false}); }
  else { state.wish = wish; state.runId = null; state.view = 'run'; chooseRun(); draw(); }
}
const classic = (surface='issues') => {
  const u=new URL(location.href);u.searchParams.set('classic','1');u.searchParams.set('surface',surface);location.assign(u.toString());
};
const openView = (view) => {state.view=view;state.showSidebar=false;draw();};
function mount() {
  // Opt-in only: classic stays the default until visual sign-off (see workbenchEnabled).
  if (!workbenchEnabled()) {
    const surface=params.get('surface');
    if (surface && ['issues','agents','allocation','settings'].includes(surface)) {
      document.querySelector(`.surface[data-surface="${surface}"]`)?.click();
    }
    return;
  }
  const root=el('div',{id:'ww',class:'ww-root',dataset:{testid:'run-workbench'}});
  root.innerHTML=`<div class="ww-layout">
    <aside class="ww-sidebar" id="ww-sidebar" aria-label="Repository runs">
      <div class="ww-brand"><span class="ww-logo">◇</span><strong>Auto Office</strong><button class="ww-icon-button ww-collapse" id="ww-sidebar-toggle" aria-label="Toggle sidebar">${icon('panel')}</button></div>
      <label class="ww-search">${icon('search')}<input id="ww-search" type="search" placeholder="Search runs or repositories" aria-label="Search runs or repositories"><kbd>⌘K</kbd></label>
      <div class="ww-section-label">REPOSITORIES <button class="ww-icon-button" id="ww-add-work" aria-label="Browse issues">${icon('plus')}</button></div>
      <nav id="ww-run-list" class="ww-run-list" aria-label="Runs"></nav>
      <nav id="ww-secondary" class="ww-secondary-nav" aria-label="Other views"></nav>
    </aside>
    <main class="ww-main" id="ww-main" aria-label="Office workbench">
      <header class="ww-top"><button id="ww-mobile-menu" class="ww-icon-button ww-mobile" aria-label="Open repository sidebar">${icon('menu')}</button><div class="ww-breadcrumb" id="ww-breadcrumb"></div><div class="ww-top-right" id="ww-status"></div><button id="ww-inspector-toggle" class="ww-icon-button" aria-label="Toggle inspector">${icon('panel')}</button></header>
      <div class="ww-work" id="ww-work"></div>
    </main>
    <aside class="ww-inspector" id="ww-inspector" aria-label="Run inspector"></aside>
    <div class="ww-overlay" id="ww-overlay" hidden></div>
    <div class="ww-command" id="ww-command" hidden></div>
    <div class="ww-toast-area" id="ww-toasts" role="status" aria-live="polite"></div>
  </div>`;
  document.body.prepend(root);document.body.dataset.workbench='true';
  const ui=loadUi();state.query=String(ui.query||'');state.issueQuery=String(ui.issueQuery||'');
  if(typeof ui.inspector==='boolean'&&!matchMedia('(max-width:1100px)').matches)state.showInspector=ui.inspector;
  if(ui.sidebarMini)root.classList.add('ww-sidebar-mini');
  $('ww-search').value=state.query;state.wish=wishFromHash();
  $('ww-search').addEventListener('input',(ev)=>{state.query=ev.target.value;saveUi();renderSidebar();});
  $('ww-add-work').addEventListener('click',()=>openView('issues'));
  $('ww-sidebar-toggle').addEventListener('click',()=>{root.classList.toggle('ww-sidebar-mini');saveUi();});
  $('ww-mobile-menu').addEventListener('click',()=>{state.showSidebar=!state.showSidebar;draw();});
  $('ww-inspector-toggle').addEventListener('click',()=>{state.showInspector=!state.showInspector;saveUi();draw();});
  $('ww-overlay').addEventListener('click',()=>{state.showSidebar=false;state.showInspector=false;draw();});
  document.addEventListener('keydown',(ev)=>{
    if ((ev.metaKey||ev.ctrlKey)&&ev.key.toLowerCase()==='k') {ev.preventDefault();togglePalette();}
    if(ev.key==='Escape' && state.palette){state.palette=false;drawPalette();}
    else if(ev.key==='Escape'&&state.showSidebar){state.showSidebar=false;draw();}
  });
  store?.subscribe?.(()=>{chooseRun();schedule();fetchActivity();});
  const narrow = matchMedia('(max-width:1100px)');
  narrow.addEventListener('change',(event)=>{if(event.matches)state.showInspector=false;draw();});
  window.addEventListener('popstate',applyHash);
  chooseRun();draw();fetchActivity();
}
function schedule() {if(state.updatePending)return;state.updatePending=true;requestAnimationFrame(()=>{state.updatePending=false;draw();});}
function draw() {
  const root=$('ww');if(!root)return;
  chooseRun();root.classList.toggle('ww-sidebar-open',state.showSidebar);
  root.classList.toggle('ww-inspector-hidden',!state.showInspector||state.view!=='run');
  $('ww-overlay').hidden=!(state.showSidebar||(state.showInspector&&matchMedia('(max-width:1100px)').matches&&state.view==='run'));
  renderSidebar();renderStatus();renderMain();renderInspector();
}
function renderSidebar() {
  const nav=$('ww-run-list');if(!nav)return;
  const search=$('ww-search');const focus=document.activeElement===search;const caret=focus?search.selectionStart:null;
  const query=state.query.trim().toLowerCase();const repos=new Map();
  for(const run of sortRuns(allRuns())) {const key=run.repo?.key || repoLabel(run);if(!repos.has(key))repos.set(key,[]);repos.get(key).push(run);}
  nav.replaceChildren();
  if(!repos.size)nav.append(el('p',{class:'ww-empty-note',text:store.state?'No Office runs yet. Open the Issue Inbox to start one.':'Connecting to local Office…'}));
  for (const [key,runs] of [...repos.entries()].sort((a,b)=>repoLabel(a[1][0]).localeCompare(repoLabel(b[1][0])))) {
    const label=repoLabel(runs[0]);const matchRepo=label.toLowerCase().includes(query);
    const matches=runs.filter(r=>matchRepo||(`${titleFor(r)} ${r.run_id} ${issueLabel(r)}`).toLowerCase().includes(query));
    if(!matches.length)continue;
    if(!state.expanded.has(key)&&!state.expanded.size&&!state.runId)state.expanded.add(key);
    const expanded=state.expanded.has(key)||!!query;
    const head=button([simple('folder'),text(label,'ww-repo-name'),text(String(runs.length),'ww-count')],()=>{if(state.expanded.has(key))state.expanded.delete(key);else state.expanded.add(key);renderSidebar();},'ww-repo',{ 'aria-expanded':String(expanded),title:label });
    nav.append(head);
    if(!expanded)continue;
    for(const r of matches){const [status,tone]=statusFor(r);const line=button([
      el('span',{class:`ww-run-mark ${tone}`}),
      el('span',{class:'ww-run-title'},text(`${issueLabel(r)}  ${titleFor(r)}`,'ww-ellip'),text(r.phase||'Phase unknown','ww-run-sub')),
      el('span',{class:'ww-run-state',text:status})
    ],()=>selectRun(r.id),`ww-run ${state.view==='run'&&state.runId===r.id?'selected':''}`,{dataset:{testid:'wb-run',runId:r.id},'aria-current':state.view==='run'&&state.runId===r.id?'page':'false',title:`${r.run_id} · ${titleFor(r)} · ${status}`});
    nav.append(line);}
  }
  const secondary=$('ww-secondary');secondary.replaceChildren();
  for(const [name,label,ic] of [['issues','Issue Inbox','inbox'],['agents','Agents','agents'],['allocation','Allocation','chart'],['settings','Settings','settings']]) {
    secondary.append(button([simple(ic),text(label)],()=>openView(name),`ww-nav ${state.view===name?'selected':''}`,{dataset:{testid:`wb-nav-${name}`}}));
  }
  secondary.append(button([simple('terminal'),text('Classic controls')],()=>classic(),'ww-nav ww-classic',{title:'Open the original Office Workstation'}));
  if(focus){search.focus({preventScroll:true});if(caret!=null)search.setSelectionRange(caret,caret);}
}
function renderStatus() {
  const s=store?.state, r=selected();const bc=$('ww-breadcrumb');const status=$('ww-status');bc.replaceChildren();status.replaceChildren();
  bc.append(text(state.view==='run'?(r?repoLabel(r):'Runs'):({issues:'Issue Inbox',agents:'Agents',allocation:'Allocation',settings:'Settings'}[state.view]||'Workbench'),'ww-bc-primary'));
  if(state.view==='run'&&r)bc.append(text('/','ww-sep'),text(`Run ${shortRun(r.run_id)}`,'ww-bc-run'),external(issueLabel(r),r.issue?.url));
  const f=s?.freshness||{};const office=f.office?.state||store?.status||'connecting';const gh=f.github?.state||'unknown';
  status.append(badge(`Office · ${office}`,office==='live'?'live':office==='stale'?'warn':'quiet'),badge(`GitHub · ${gh}`,gh==='fresh'?'done':gh==='rate_limited'?'warn':'quiet'));
  if(fixture) status.append(text(`FIXTURE: ${fixture}`,'ww-fixture'));
}
function renderMain() {
  const wrap=$('ww-work');if(!wrap)return;
  const previous=wrap.dataset.renderedRun;
  const oldScroll=$('ww-feed')?.scrollTop;
  if(previous&&oldScroll!=null)state.runScroll.set(previous,oldScroll);
  const active=document.activeElement;
  const restoring=['ww-composer-input','ww-issue-search'].includes(active?.id)
    ? {id:active.id,start:active.selectionStart,end:active.selectionEnd} : null;
  wrap.dataset.renderedRun=state.view==='run'?state.runId||'':'';
  wrap.replaceChildren();
  if(state.view==='run')renderRun(wrap);else if(state.view==='issues')renderInbox(wrap);
  else if(state.view==='agents')renderGlobalAgents(wrap);
  else if(state.view==='allocation')renderAllocation(wrap);else renderSettings(wrap);
  if(restoring){const next=$(restoring.id);if(next&&!next.disabled){next.focus({preventScroll:true});next.setSelectionRange(restoring.start,restoring.end);}}
}
function renderRun(wrap) {
  if(state.notFound&&!selected()){const nf=state.notFound;wrap.append(el('section',{class:'ww-empty',dataset:{testid:'wb-run-not-found'}},el('h1',{text:'Run not found'}),el('p',{text:`No Office run matches ${nf.repo?`${nf.repo} / `:''}${nf.run}. It may have been removed, or this link is from another machine.`}),button('Choose a run',()=>{state.notFound=null;state.runId=null;chooseRun();history.replaceState(null,'',location.pathname+location.search);draw();},'ww-primary'),button('Open Issue Inbox',()=>openView('issues'),'ww-secondary')));return;}
  const r=selected();if(!r){wrap.append(el('section',{class:'ww-empty'},el('h1',{text:'No runs yet'}),el('p',{text:'Your active and historical Office runs will appear here. GitHub issues without runs stay in the Issue Inbox.'}),button('Open Issue Inbox',()=>openView('issues'),'ww-primary')));return;}
  const status=statusFor(r);
  const article=el('section',{class:'ww-thread'});
  const head=el('header',{class:'ww-thread-header'},el('h1',{text:titleFor(r)}),el('p',{text:`${repoLabel(r)} · ${r.goal||'Office run'}`}),el('div',{class:'ww-meta'},badge(status[0],status[1]),badge(r.phase||'Phase unknown','quiet'),text(`Updated ${when(r.updated_at||r.created_at)}`,'ww-muted'),button('Copy link',()=>copy(linkFor(r)),'ww-small',{dataset:{testid:'wb-copy-link'}})));
  article.append(head);
  if(store.status!=='live'||store.state?.freshness?.office?.state!=='live')article.append(el('div',{class:'ww-warning'},simple('alert'),text('Office is not live. Data may be stale; interactive commands are unavailable.')));
  if(r.awaiting_plan_authorization){const card=el('div',{class:'ww-warning'},simple('alert'),text('Waiting for explicit plan authorization. The browser cannot grant it.'),el('code',{text:PLAN_APPROVAL_COMMAND}),button('Copy command',()=>copy(PLAN_APPROVAL_COMMAND),'ww-small'));article.append(card);}
  const feed=el('section',{class:'ww-feed',id:'ww-feed',role:'log','aria-label':'Recorded run activity'});
  const activity=state.activity.get(r.run_id);
  if(!activity||!activity.items){feed.append(el('div',{class:'ww-event-placeholder',text:'Loading recorded Office events…'}));}
  else if(activity.error){feed.append(el('div',{class:'ww-warning',text:`Activity unavailable: ${activity.error}`}));}
  else if(!activity.items.length){feed.append(el('div',{class:'ww-event-placeholder',text:activity.available===false?'No event table for this runtime.':'No recorded events yet for this run.'}));}
  else for(const event of [...activity.items].reverse()) feed.append(eventRow(event));
  for(const sent of state.localSends.get(r.id)||[]){sent.status=receiptStatus(sent);feed.append(el('article',{class:'ww-event ww-user-event'},el('span',{class:'ww-event-glyph ww-purple'},simple('send')),el('div',{class:'ww-event-body'},el('div',{class:'ww-event-meta'},text('You → orchestrator','ww-strong'),text(when(sent.at),'ww-muted')),el('p',{text:sent.text}),badge(sent.status,sent.status==='completed'?'done':sent.status==='unknown'||sent.status==='failed'?'warn':'quiet'))));}
  feed.append(el('div',{class:'ww-end-marker',text:'Only recorded Office events are shown. Provider transcripts are not synthesized.'}));
  article.append(feed);wrap.append(article);
  requestAnimationFrame(()=>{const target=$('ww-feed');if(target&&state.view==='run'){const old=state.runScroll.get(r.id);target.scrollTop=old===undefined?target.scrollHeight:old;}});
  renderComposer(article,r);
}
function eventRow(evt) {
  const kind=String(evt.kind||'event');const review=/review|gate|verdict/i.test(kind);const acceptance=/accept|complete|land|close/i.test(kind);
  const row=el('article',{class:'ww-event',dataset:{testid:'wb-event',seq:String(evt.seq)}},el('span',{class:`ww-event-glyph ${acceptance?'ww-green':review?'ww-purple':'ww-blue'}`},simple(acceptance?'check':review?'agents':'clock')));
  const body=el('div',{class:'ww-event-body'},el('div',{class:'ww-event-meta'},text(kind.replace(/[._]/g,' '),'ww-strong'),text(when(evt.created_at),'ww-muted'),evt.task_id?badge(evt.task_id,'quiet'):null),el('p',{text:evt.summary||'Event recorded without a summary.'}));
  if(evt.payload!==null&&evt.payload!==undefined){const details=el('details',{class:'ww-event-details'},el('summary',{text:'Recorded details'}),el('pre',{text:JSON.stringify(evt.payload,null,2)}));body.append(details);}
  row.append(body);return row;
}
const agentsFor = (r) => {
  const entities=Object.values(store.state?.entities?.agents||{}).filter(a=>a.run===r.id);
  if(entities.length)return entities;
  return Object.entries(r.agents?.columns||{}).flatMap(([column,list])=>(list||[]).map(a=>({...a,run:r.id,column})));
};
function exactOrchestrator(r) {
  const owner=r.owner;if(!owner||owner.kind!=='session'||!owner.id)return null;
  return agentsFor(r).find(a=>a.id===owner.id&&a.column==='orchestrators'&&a.kind==='session')||null;
}
function composerTarget(r) {
  const node=exactOrchestrator(r);
  if(!node)return {node:null,target:null,blocked:'No active orchestrator session is bound to this run. Attach or resume through Office to send a message.'};
  const blocked=chatBlocked(store.state,store.status,node);
  const target=chatTarget(store.state,node);
  return {node,target,blocked:blocked||(!target?.run_id||!target?.session?'The exact session target is unavailable.':null)};
}
function renderComposer(article,r) {
  const {node,target,blocked}=composerTarget(r);
  const key=target?targetKey(target):`unbound:${r.id}`;
  const row=el('section',{class:'ww-composer-wrap',dataset:{testid:'wb-composer'}},el('div',{class:'ww-composer'},el('label',{class:'ww-sr',for:'ww-composer-input',text:'Message the active orchestrator'})));
  const area=el('textarea',{id:'ww-composer-input',rows:'2',placeholder:blocked?'No live orchestrator · read-only history':'Message the orchestrator…','aria-label':'Message the orchestrator',disabled:!!blocked});
  area.value=state.drafts.get(key)||'';
  area.addEventListener('input',()=>{state.drafts.set(key,area.value);});
  area.addEventListener('keydown',(ev)=>{if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();sendMessage(r.id,target,key,area.value);}});
  const bar=el('div',{class:'ww-composer-footer'},el('div',{class:'ww-route-info'},simple('agents'),text(node?`${node.harness||'Harness unknown'} · ${node.model||'model unavailable'} · ${node.effort||'effort unavailable'}`:'Session unavailable','ww-muted')));
  bar.append(button([simple('send'),text('Send')],()=>sendMessage(r.id,target,key,area.value),'ww-primary',{disabled:!!blocked||state.pending.has(key),dataset:{testid:'wb-send'}}));
  row.firstChild.append(area,bar);
  row.append(el('div',{class:'ww-composer-note',text:blocked||'Messages go only to the exact live orchestrator binding. Worker/reviewer chat is unavailable.'}));
  if(blocked){const acts=el('div',{class:'ww-actions'});for(const kind of ['attach_run','resume_run']){const cap=r.controls?.[kind];if(cap?.allowed)acts.append(button(kind==='attach_run'?'Attach to run':'Resume run',()=>runAction(kind,r),'ww-secondary'));}if(acts.childNodes.length)row.append(acts);}
  article.append(row);
}
function commandId() {return `web-${globalThis.crypto?.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('')}`;}
async function sendCommand(kind,target,payload={},expect={}) {
  const id=commandId();let response,body;
  try {response=await fetch('/api/commands',{method:'POST',headers:{'Content-Type':'application/json','X-Office-Token':token},body:JSON.stringify({id,kind,target,payload,expect})});body=await response.json().catch(()=>null);}
  catch {return {status:'unknown',error:'No service response. Check the command receipt before retrying.',id};}
  if(response.ok&&body?.receipt)return {status:body.receipt.status||'pending',error:body.receipt.error||null,id};
  if(response.status<500) return {status:'failed',error:body?.reason||body?.message||`HTTP ${response.status}`,id};
  return {status:'unknown',error:'Service outcome unknown; do not retry without checking its receipt.',id};
}
// The server receipt (entities.commands, same source classic chat.js reads) is authoritative. A
// non-terminal local status with no terminal receipt must not read "pending" forever, so it
// degrades to "unknown" (check the receipt; never auto-retry) after RECEIPT_WAIT_MS.
const RECEIPT_WAIT_MS = 30000;
const TERMINAL = new Set(['completed', 'failed', 'unknown', 'checked']);
function receiptStatus(sent) {
  const server = sent.id && store.state?.entities?.commands?.[`command:${sent.id}`]?.status;
  if (server && TERMINAL.has(server)) return server;
  const status = server || sent.status;
  if (TERMINAL.has(status)) return status;
  if (sent.id && Date.now() - Date.parse(sent.at) > RECEIPT_WAIT_MS) return 'unknown';
  return status;
}
async function sendMessage(runId,target,key,body) {
  const message=String(body||'').trim();if(!message||!target||state.pending.has(key))return;
  const r=store.state?.entities?.runs?.[runId];if(!r)return;
  const current=composerTarget(r);
  if(current.blocked||!current.target||targetKey(current.target)!==key||state.runId!==runId||store.status!=='live'){toast('Target changed or not live. The message was not sent.','warn');return;}
  state.pending.add(key);const sent={text:message,at:new Date().toISOString(),status:'pending',id:null};
  state.localSends.set(runId,[...(state.localSends.get(runId)||[]),sent]);state.drafts.delete(key);draw();
  const result=await sendCommand('chat_send',{host:target.host,run_id:target.run_id,session:target.session},{text:message},{});
  sent.id=result.id;sent.status=result.status;if(!TERMINAL.has(result.status))setTimeout(()=>{if(state.localSends.has(runId))schedule();},RECEIPT_WAIT_MS+500);if(result.status==='failed'){sent.error=result.error;toast(`Message refused: ${result.error}`,'warn');state.drafts.set(key,message);}else if(result.status==='unknown')toast(result.error,'warn');
  state.pending.delete(key);draw();
}
async function runAction(kind,r){if(!r?.controls?.[kind]?.allowed||store.state?.freshness?.office?.state!=='live'){toast('Office has not authorized this command.','warn');return;}const result=await sendCommand(kind,{run_id:r.run_id});toast(`${kind.replace('_',' ')}: ${result.status}${result.error?` (${result.error})`:''}`,result.status==='failed'?'warn':'info');}
function card(title,contents,cls='') {return el('section',{class:`ww-card ${cls}`},el('h3',{text:title}),contents);}
function kv(label,value){return el('div',{class:'ww-kv'},el('dt',{text:label}),el('dd',{},typeof value==='string'?text(value):value));}
function renderInspector() {
  const pane=$('ww-inspector');if(!pane)return;
  const previousKey=pane.dataset.renderedKey;
  const previousScroll=pane.querySelector('.ww-inspector-scroll')?.scrollTop||0;
  pane.replaceChildren();if(state.view!=='run'||!state.showInspector)return;
  const r=selected();if(!r)return;
  const key=`${r.id}:${state.inspector}`;pane.dataset.renderedKey=key;
  const tabs=el('nav',{class:'ww-inspector-tabs',role:'tablist','aria-label':'Run details'});
  for(const [tab,label] of [['overview','Overview'],['tasks','Tasks'],['agents','Agents'],['changes','Changes'],['activity','Activity']]){
    tabs.append(button(label,()=>{state.inspector=tab;renderInspector();},`ww-tab ${state.inspector===tab?'selected':''}`,{role:'tab','aria-selected':String(state.inspector===tab),dataset:{testid:`wb-tab-${tab}`}}));
  }
  pane.append(tabs);const scroll=el('div',{class:'ww-inspector-scroll'});pane.append(scroll);
  if(key===previousKey)requestAnimationFrame(()=>{if(scroll.isConnected)scroll.scrollTop=previousScroll;});
  if(state.inspector==='overview'){
    const [status,tone]=statusFor(r),p=r.progress;
    scroll.append(card('Run details',el('dl',{class:'ww-kvs'},kv('Repository',repoLabel(r)),kv('Issue',external(issueLabel(r),r.issue?.url)),kv('Run ID',r.run_id),kv('Status',badge(status,tone)),kv('Phase',r.phase||'Unavailable'),kv('Updated',when(r.updated_at||r.created_at)),kv('Office version',r.office_version||'Unknown'))));
    scroll.append(card('Verified progress',el('div',{class:'ww-progress'},el('div',{class:'ww-progress-track'},el('span',{style:`width:${p?.value!=null?Math.max(0,Math.min(100,p.value*100)):0}%`})),el('p',{text:p?`${p.accepted_weight} / ${p.total_weight} accepted tasks`:'No task progress recorded'}))));
    const taskCount=(r.tasks||[]).length;scroll.append(card('Tasks',el('div',{class:'ww-summary'},text(`${taskCount} recorded tasks`),button('Inspect',()=>{state.inspector='tasks';renderInspector();},'ww-small'))));
    scroll.append(card('Active agents',el('div',{class:'ww-summary'},text(`${agentsFor(r).length} projected agents`),button('Inspect',()=>{state.inspector='agents';renderInspector();},'ww-small'))));
    scroll.append(card('Linked PRs',el('div',{class:'ww-summary'},text(`${(r.prs||[]).length} Office PR references`),button('Inspect',()=>{state.inspector='changes';renderInspector();},'ww-small'))));
  }else if(state.inspector==='tasks'){
    const tasks=r.tasks||[];scroll.append(el('h3',{class:'ww-list-heading',text:`TASKS · ${tasks.length}`}));
    if(!tasks.length)scroll.append(el('p',{class:'ww-empty-note',text:'No recorded tasks for this run.'}));
    for(const task of tasks){scroll.append(card(`${task.task_id} · ${task.title||'Untitled task'}`,el('div',{class:'ww-task-body'},badge(task.status||'Unknown',task.status==='accepted'?'done':task.status==='blocked'?'warn':'quiet'),el('p',{text:(task.depends||[]).length?`Depends on ${(task.depends||[]).join(', ')}`:'No dependencies recorded'}),el('p',{text:`Route: ${task.route?.dispatched||task.route?.primary||'unavailable'}`}))));}
  }else if(state.inspector==='agents'){
    const all=agentsFor(r);scroll.append(el('h3',{class:'ww-list-heading',text:`AGENTS · ${all.length}`}));
    if(!all.length)scroll.append(el('p',{class:'ww-empty-note',text:'No projected agents for this run.'}));
    for(const agent of all){const st=agent.state||{};const stateName=st.quota_wait?.active?'Quota wait':st.unavailable?'Unavailable':st.complete?'Completed':st.paused?'Paused':st.blocked?'Blocked':st.activity||st.process||'Unknown';
      scroll.append(card(agent.role||agent.column||'Agent',el('div',{class:'ww-agent-body'},el('p',{text:`${agent.harness||'Unknown harness'} · ${agent.model||'Model unavailable'} · ${agent.effort||'Effort unavailable'}`}),badge(stateName,st.unavailable?'warn':st.process==='alive'?'live':'quiet'),el('p',{text:agent.kind==='session'?'Orchestrator session · messaging only when an exact live binding exists':'Worker/reviewer · inspect only'}))));}
  }else if(state.inspector==='changes'){
    scroll.append(el('h3',{class:'ww-list-heading',text:'PR REFERENCES FROM OFFICE'}));
    const prs=r.prs||[];if(!prs.length)scroll.append(el('p',{class:'ww-empty-note',text:'No task PRs recorded for this run.'}));
    for(const pr of prs){const gh=pr.ref&&store.state?.entities?.prs?.[pr.ref];const stateText=gh?.github?.state||(pr.merged?'Merged':'GitHub state unavailable');
      scroll.append(card(`PR #${pr.number||'?'}`,el('div',{class:'ww-change-body'},el('p',{text:`${pr.branch||'Branch unknown'} → ${pr.base||'Base unknown'}`}),el('p',{text:`${stateText} · Office gate and GitHub checks are separate.`}),external('Open on GitHub ↗',pr.url))));}
  }else {
    const activity=state.activity.get(r.run_id);scroll.append(el('h3',{class:'ww-list-heading',text:'RECORDED OFFICE ACTIVITY'}));
    if(activity?.items?.length)for(const evt of activity.items.slice(0,25))scroll.append(el('div',{class:'ww-activity-row'},text(evt.kind||'event','ww-strong'),text(when(evt.created_at),'ww-muted')));
    else scroll.append(el('p',{class:'ww-empty-note',text:'No events available from Office.'}));
    scroll.append(button('Refresh activity',()=>fetchActivity(true),'ww-secondary'));
  }
}
function fetchActivity(force=false) {
  const r=selected();if(!r||state.view!=='run'||!store?.state)return;
  const entry=state.activity.get(r.run_id), now=Date.now();
  if(!force&&(entry?.loading||(entry?.at&&now-entry.at<15000)))return;
  state.activity.set(r.run_id,{...(entry||{}),loading:true,at:now});
  const seq=++state.activitySequence;
  fetch(`/api/runs/${encodeURIComponent(r.run_id)}/activity?limit=80`,{cache:'no-store'})
  .then(async resp=>{if(!resp.ok)throw new Error(`HTTP ${resp.status}`);return resp.json();})
  .then(data=>{if(seq===state.activitySequence||selected()?.run_id===r.run_id){state.activity.set(r.run_id,{items:Array.isArray(data.items)?data.items:[],available:data.available!==false,error:data.reason||null,loading:false,at:Date.now()});schedule();}})
  .catch(err=>{state.activity.set(r.run_id,{items:entry?.items||[],available:false,error:String(err),loading:false,at:Date.now()});schedule();});
}
function renderInbox(wrap) {
  const s=store.state;const panel=el('section',{class:'ww-page'},el('div',{class:'ww-page-head'},el('h1',{text:'Issue Inbox'}),el('p',{text:'Find GitHub issues, identify launch readiness, and open existing Office runs.'})));
  const line=el('div',{class:'ww-toolbar'},el('input',{type:'search',placeholder:'Search issues and repositories','aria-label':'Search issues',id:'ww-issue-search'}),button('Manage intake in Classic',()=>classic('issues'),'ww-secondary'));
  line.querySelector('input').value=state.issueQuery;line.querySelector('input').addEventListener('input',(e)=>{state.issueQuery=e.target.value;saveUi();const pos=e.target.selectionStart;renderMain();const latest=$('ww-issue-search');latest?.focus({preventScroll:true});latest?.setSelectionRange(pos,pos);});panel.append(line);
  const rows=issueRows(s||{entities:{},freshness:{}}).filter(i=>`${i.repoName} #${i.number} ${i.title||''}`.toLowerCase().includes(state.issueQuery.toLowerCase()));
  const table=el('div',{class:'ww-inbox-list'});for(const i of rows.slice(0,400)){
    const runs=i.runs||[];
    table.append(el('article',{class:'ww-inbox-row'},el('div',{},el('h3',{text:`#${i.number} ${i.title||'Title unavailable'}`}),el('p',{text:`${i.repoName} · ${i.runIds?.length||0} linked runs · ${i.phase}`})),el('div',{class:'ww-inbox-action'},openRunControl(runs))));}
  if(!rows.length)table.append(el('p',{class:'ww-empty-note',text:'No matching issues.'}));if(rows.length>400)table.append(el('p',{class:'ww-empty-note',text:'Showing 400 issues. Narrow your search to see other results.'}));
  panel.append(table);wrap.append(panel);
}
// One run opens directly. Several runs need an explicit choice: never guess which one the user meant.
function openRunControl(runs) {
  if(!runs.length)return button('Start / queue',()=>classic('issues'),'ww-secondary');
  if(runs.length===1)return button('Open run',()=>selectRun(runs[0].id),'ww-secondary');
  const box=el('div',{class:'ww-run-choice',dataset:{testid:'wb-run-choice'}},text(`${runs.length} runs, choose one`,'ww-muted'));
  for(const r of sortRuns(runs))box.append(button(`${shortRun(r.run_id)} · ${statusFor(r)[0]}`,()=>selectRun(r.id),'ww-small',{dataset:{testid:'wb-open-run',runId:r.id},title:`${r.run_id} · ${r.phase||'Phase unknown'}`}));
  return box;
}
function renderGlobalAgents(wrap) {
  const panel=el('section',{class:'ww-page'},el('div',{class:'ww-page-head'},el('h1',{text:'Agents'}),el('p',{text:'Inspect actual Office sessions and dispatches. Messaging is only available to active orchestrators in their run.'})));
  const agents=Object.values(store.state?.entities?.agents||{}), list=el('div',{class:'ww-agent-list'});
  for(const a of agents){const r=store.state.entities.runs[a.run];const stateName=a.state?.unavailable?'Unavailable':a.state?.complete?'Completed':a.state?.activity||a.state?.process||'Unknown';
    list.append(el('article',{class:'ww-compact-card'},el('div',{},el('h3',{text:a.role||a.column||'Agent'}),el('p',{text:`${r?titleFor(r):'Run unavailable'} · ${a.harness||'Harness unknown'} · ${a.model||'Model unavailable'}`})),badge(stateName,a.state?.unavailable?'warn':'quiet'),button('Inspect parent run',()=>r&&selectRun(r.id),'ww-small',{disabled:!r})));
  }
  if(!agents.length)list.append(el('p',{class:'ww-empty-note',text:'No agent sessions or dispatches recorded.'}));panel.append(list,button('Open role graph / controls',()=>classic('agents'),'ww-secondary'));wrap.append(panel);
}
function renderAllocation(wrap) {
  const panel=el('section',{class:'ww-page'},el('div',{class:'ww-page-head'},el('h1',{text:'Allocation'}),el('p',{text:'Machine-wide scheduler projection. Queue and execution authority remain in the Office runtime.'})));
  const list=el('div',{class:'ww-agent-list'}), items=Object.values(store.state?.entities?.queue||{});
  if(!items.length)list.append(el('p',{class:'ww-empty-note',text:'The Office scheduler has no queued items.'}));
  for(const item of items)list.append(el('article',{class:'ww-compact-card'},el('div',{},el('h3',{text:item.title||item.ref||item.id||'Queue item'}),el('p',{text:`${item.kind||'Unknown kind'} · ${item.status||'Status unknown'} · priority ${item.priority||'unknown'}`})),badge(item.paused?'Paused':item.status||'Queued',item.paused?'warn':'quiet')));
  panel.append(list,button('Manage queue in Classic',()=>classic('allocation'),'ww-secondary'));wrap.append(panel);
}
function renderSettings(wrap) {
  const panel=el('section',{class:'ww-page'},el('div',{class:'ww-page-head'},el('h1',{text:'Settings'}),el('p',{text:'Office policy is still governed by the runtime, not by browser preferences.'})));
  panel.append(card('Runtime and settings',el('div',{},el('p',{text:'Machine, repository and run-pinned settings retain their current precedence. Changing a setting requires an Office-authorized command.'}),button('Open Classic Settings',()=>classic('settings'),'ww-secondary'))));
  panel.append(card('Navigation',el('div',{},el('p',{text:'This run-first workbench is opt-in. The original Workstation remains accessible for full operational controls.'}),button('Open Classic Workstation',()=>classic(),'ww-secondary'))));
  const on=store_.get(MODE_KEY)==='workbench';
  panel.append(card('Default interface',el('div',{},el('p',{text:on?'The run-first workbench opens by default in this browser. Add ?classic=1 to a URL to open the original Workstation once.':'The original Workstation opens by default. The workbench is opt-in until maintainer sign-off; use ?workbench=1 for a single visit or turn it on for this browser.'}),button(on?'Make Classic the default':'Make workbench the default',()=>{store_.set(MODE_KEY,on?null:'workbench');draw();toast(on?'Classic is the default again.':'Workbench will open by default in this browser.');},'ww-secondary',{dataset:{testid:'wb-mode-toggle'}}))));
  wrap.append(panel);
}
function togglePalette(){state.palette=!state.palette;drawPalette();}
function drawPalette() {
  const mount=$('ww-command');if(!mount)return;mount.hidden=!state.palette;if(!state.palette){mount.replaceChildren();return;}
  mount.replaceChildren();const dialog=el('section',{class:'ww-palette-dialog',role:'dialog','aria-label':'Command palette','aria-modal':'true'}),q=el('input',{type:'search',placeholder:'Search runs and views…',id:'ww-palette-input','aria-label':'Search commands'});
  const list=el('div',{class:'ww-palette-list'});const fill=()=>{list.replaceChildren();const options=[...sortRuns(allRuns()).map(r=>({label:`${issueLabel(r)}  ${titleFor(r)}`,hint:repoLabel(r),action:()=>selectRun(r.id)})),...[['Issue Inbox','issues'],['Agents','agents'],['Allocation','allocation'],['Settings','settings']].map(([label,v])=>({label,hint:'Workspace',action:()=>openView(v)}))];for(const entry of options.filter(o=>`${o.label} ${o.hint}`.toLowerCase().includes(q.value.toLowerCase())).slice(0,60))list.append(button([text(entry.label),text(entry.hint,'ww-muted')],()=>{state.palette=false;entry.action();drawPalette();},'ww-palette-item'));if(!list.childElementCount)list.append(el('p',{class:'ww-empty-note',text:'No matching runs or views'}));};
  q.addEventListener('input',fill);q.addEventListener('keydown',(ev)=>{if(ev.key==='Enter'){const first=list.querySelector('button');if(first){ev.preventDefault();first.click();}}});
  dialog.append(q,list);mount.append(el('div',{class:'ww-palette-shade',onclick:()=>{state.palette=false;drawPalette();}}),dialog);fill();q.focus();
}
function toast(message,tone='info'){const root=$('ww-toasts');if(!root)return;const node=el('div',{class:`ww-toast ${tone}`,text:message});root.append(node);setTimeout(()=>node.remove(),5500);}
async function copy(value){try{await navigator.clipboard.writeText(value);toast('Copied to clipboard');}catch{toast('Copy unavailable in this browser','warn');}}
mount();