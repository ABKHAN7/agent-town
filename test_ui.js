// Checks index.html's script: build() should only ever run once, no matter how many polls happen.
// There used to be infinite recursion here (pick -> render -> build) that ate 4GB and crashed.
const fs = require('fs'), assert = require('assert'), vm = require('vm');

const html = fs.readFileSync(__dirname + '/index.html', 'utf8');
const code = html.split('<script>')[1].split('</script>')[0];

let gridWrites = 0;
const mkEl = id => ({
  id, _html: '', style: {setProperty(){}}, classList: {_c: new Set(), toggle(c){ this._c.has(c) ? this._c.delete(c) : this._c.add(c); },
    add(c){ this._c.add(c); }, remove(c){ this._c.delete(c); }, contains(c){ return this._c.has(c); }},
  dataset: {a: 'desk-1'}, textContent: '', value: '', hidden: false, disabled: false,
  placeholder: '', className: '', scrollTop: 0, clientHeight: 0, scrollHeight: 0,
  href: '', onclick: null, onkeydown: null, remove(){}, addEventListener(){}, setAttribute(){},
  getBoundingClientRect(){ return {left: 0, top: 0, width: 60, height: 60}; },
  appendChild(){},
  set innerHTML(v){ if(this.id === 'grid') gridWrites++; this._html = v; },
  get innerHTML(){ return this._html; },
  querySelector(){ return mkEl('x'); },
  querySelectorAll(){ return []; },
});
const els = {};
const get = id => (els[id] = els[id] || mkEl(id));
let badToasts = [];
els['toasts'] = mkEl('toasts');
els['toasts'].appendChild = node => {
  if(String(node.className || '').includes('bad')) badToasts.push(node.textContent);
};

const state = {
  project: '/repo', base: 'saad-dev', now: 0,
  types: [{id: 'module', label: 'New module'}],
  shipper: {agent: 'shipper', status: 'empty', reviewed: '', verdict: '', detail: ''},
  desks: ['desk-1','desk-2','desk-3'].map(a => ({
    agent: a, status: 'empty', tool: '', detail: '', task: '', ttype: '',
    turns: 0, cost: 0, report: null, started: 0, log: [], output: '',
    branch: 'fleet/' + a, changed: 0,
    tokens: {in: 1000, out: 2000, cache_read: 90000, cache_write: 10000}})),
  usage: {
    day: '2026-08-08',
    today: {in: 1234, out: 5678, cache_read: 2000000, cache_write: 100000,
            cost: 1.5, runs: 4, total: 2106912, cache_hit: 95.2, per_run: 526728,
            cost_per_run: 0.375},
    total: {in: 1234, out: 5678, cache_read: 2000000, cache_write: 100000,
            cost: 1.5, runs: 4, total: 2106912, cache_hit: 95.2, per_run: 526728,
            cost_per_run: 0.375},
    models: {'claude-sonnet-5': {runs: 4, total: 2106912, cache_hit: 95.2, cost: 1.5}},
    plan: {plan: 'pro', auth: 'claude.ai', email: 'a@b.c'},
  },
  conflicts: {},
};
let usageHistoryResponse = {history: [
  {ts: Date.now()/1000 - 90, desk: 'desk-1', name: 'Byte', task: 'add attendance summary',
   ttype: 'feature', model: 'claude-sonnet-5', tokens: {in: 10, out: 20, cache_read: 100, cache_write: 5}, cost: 0.12},
]};
let selfGitResponse = {branch: 'main', remote: 'git@github.com:ABKHAN7/agent-town.git',
  dirty: [], ahead: 0, behind: 0, last_commit: {hash: 'abc123', subject: 'init', when: '2h ago', author: 'saad'}};
let selfGitProjectResponse = {branch: 'saad-dev', remote: 'git@github.com:x/RastgarEngineering.git',
  dirty: ['website_rastgar/models/x.py'], dirty_count: 1, ahead: 0, behind: 0,
  last_commit: {hash: 'def789', subject: 'wip', when: '1h ago', author: 'saad_361'},
  branches: ['main', 'stage', 'saad-dev', 'usama1']};
let eventsResponse = {events: [
  {ts: Date.now()/1000 - 30, kind: 'task_done', desk: 'desk-1', message: 'finished ok', level: 'info'},
  {ts: Date.now()/1000 - 10, kind: 'ship_error', desk: 'desk-2', message: 'push rejected', level: 'error'},
]};

const store = {};
let prResponse = {ok: true, url: 'https://github.com/x/y/pull/9'};
const openedUrls = [];
let notifPermission = 'default';
const notificationsSpawned = [];
class FakeNotification {
  constructor(title){ notificationsSpawned.push(title); }
  static requestPermission(){ notifPermission = 'granted'; return Promise.resolve(notifPermission); }
  static get permission(){ return notifPermission; }
}
const ctx = {
  document: {
    getElementById: get, querySelectorAll: () => [mkEl('a')],
    documentElement: {dataset: {}}, createElement: () => mkEl('toast'),
    body: mkEl('body'), activeElement: {tagName: 'BODY', blur(){}},
    addEventListener(){}, removeEventListener(){},
    visibilityState: 'hidden', hasFocus: () => false,
  },
  getComputedStyle: () => ({getPropertyValue: () => '#fff'}),
  fetch: async (url) => url.startsWith('/usage-history') ? {ok: true, json: async () => usageHistoryResponse}
    : url.startsWith('/self-git?repo=project') ? {ok: true, json: async () => selfGitProjectResponse}
    : url.startsWith('/self-git') ? {ok: true, json: async () => selfGitResponse}
    : url.startsWith('/events') ? {ok: true, json: async () => eventsResponse}
    : url.startsWith('/pr') ? {ok: prResponse.ok, json: async () => prResponse}
    : {ok: true, json: async () => state},
  localStorage: {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }},
  setInterval: () => 0, setTimeout: () => 0, clearTimeout(){}, console,
  open: (url) => openedUrls.push(url), navigator: {mediaDevices: null, clipboard: {writeText: async () => {}}},
  confirm: () => false, addEventListener(){}, removeEventListener(){},
  Notification: FakeNotification,
  Date, Math, JSON, String, Object,
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(code, ctx);

let confettiSpawned = 0;
ctx.document.body.appendChild = () => { confettiSpawned++; };

(async () => {
  for (let i = 0; i < 20; i++) await ctx.poll();
  assert.strictEqual(gridWrites, 1,
    `build() ran ${gridWrites} time(s), should be 1 (did the recursion come back?)`);

  // desk-1 goes 'working' -> 'done' -> confetti + toast, no crash
  state.desks[0].status = 'working'; await ctx.poll();
  state.desks[0].status = 'done'; state.desks[0].turns = 5;
  await ctx.poll();
  assert.ok(confettiSpawned >= 18, `confetti particles should have spawned, got ${confettiSpawned}`);
  // rendering the same 'done' status again shouldn't re-trigger confetti
  const before = confettiSpawned;
  await ctx.poll();
  assert.strictEqual(confettiSpawned, before, 'confetti should only fire once, on status change');

  assert.deepStrictEqual(badToasts, [], 'render() silently raised an error toast: ' + badToasts.join(' | '));

  // --- token usage chip ---
  assert.ok(get('usagechip').innerHTML.includes('2.11M'),
    'usage chip should show today\'s tokens, got: ' + get('usagechip').innerHTML);
  assert.ok(get('usagechip').innerHTML.includes('95.2%'), 'usage chip should show the cache hit rate');
  // a state with no usage block at all (older server) must not throw
  const savedUsage = state.usage; state.usage = undefined;
  await ctx.poll();
  state.usage = savedUsage;

  // --- merge conflict resolver ---
  ctx.pick('desk-1');
  assert.strictEqual(get('pconflict').hidden, true, 'no conflict -> the section stays hidden');
  state.conflicts = {'desk-1': {branch: 'stage', sha: 'abc', done: [],
                                pending: ['saad-dev'], files: ['a/b.py', 'c.xml']}};
  await ctx.poll();
  assert.strictEqual(get('pconflict').hidden, false, 'a conflict must reveal the resolver');
  assert.ok(get('cffiles').innerHTML.includes('a/b.py')
    && get('cffiles').innerHTML.includes('c.xml'), 'both conflicted files should be listed');
  assert.ok(get('cfbranch').textContent.includes('stage')
    && get('cfbranch').textContent.includes('saad-dev'),
    'the resolver should name the branch being pushed and what is still queued');
  state.conflicts = {'desk-1': {branch: 'stage', sha: 'abc', done: [], pending: [], files: []}};
  await ctx.poll();
  assert.ok(get('cfnote').innerHTML.includes('resolved'),
    'with every file fixed the resolver should say it is ready to push');
  state.conflicts = {};
  await ctx.poll();
  assert.strictEqual(get('pconflict').hidden, true, 'resolved -> the section hides again');

  assert.deepStrictEqual(badToasts, [], 'the new panels raised an error toast: ' + badToasts.join(' | '));

  // --- per-hunk conflict resolution (pure text logic) ---
  const CONFLICTED = [
    "{", "  'name': 'Theme',",
    "<<<<<<< HEAD", "  'version': '17.0.7.0.0',",
    "=======", "  'version': '17.0.6.10.0',",
    ">>>>>>> ce75ca901 (New feature)",
    "  'category': 'Website',",
    "<<<<<<< HEAD", "  a = 1",
    "=======", "  a = 2",
    ">>>>>>> ce75ca901 (New feature)", "}"].join('\n');

  const hs = ctx.parseConflicts(CONFLICTED);
  assert.strictEqual(hs.length, 2, 'both conflict hunks should be found');
  assert.strictEqual(hs[0].cur.join('\n'), "  'version': '17.0.7.0.0',");
  assert.strictEqual(hs[0].inc.join('\n'), "  'version': '17.0.6.10.0',");
  assert.strictEqual(hs[0].start, 2, 'hunk should report the line it starts on');
  assert.strictEqual(ctx.parseConflicts('no markers here').length, 0);
  // a truncated hunk must be left alone rather than half-applied
  assert.strictEqual(ctx.parseConflicts('<<<<<<< HEAD\nonly ours\n').length, 0,
    'an unterminated conflict must not be treated as resolvable');

  const ed = get('filecontent');
  ed.value = CONFLICTED;
  ctx.applyHunk(0, hs[0].inc);            // accept incoming on the first hunk
  assert.ok(ed.value.includes("'version': '17.0.6.10.0',"), 'incoming side should survive');
  assert.ok(!ed.value.includes("'version': '17.0.7.0.0',"), 'current side should be gone');
  assert.ok(!ed.value.includes('<<<<<<< HEAD\n  \'version\''), 'markers of hunk 1 should be gone');
  assert.strictEqual(ctx.parseConflicts(ed.value).length, 1,
    'resolving one hunk must leave the other one intact');

  const rest = ctx.parseConflicts(ed.value);
  ctx.applyHunk(0, rest[0].cur.concat(rest[0].inc));   // accept both
  assert.ok(ed.value.includes('  a = 1') && ed.value.includes('  a = 2'),
    'accept-both should keep both sides');
  assert.strictEqual(ctx.parseConflicts(ed.value).length, 0, 'no conflicts should remain');
  assert.ok(!/[<>=]{7}/.test(ed.value), 'no marker line may survive');
  assert.strictEqual(get('hunks').hidden, true, 'the hunk panel hides once the file is clean');

  // highlightCode() - regex-based tokenizer for the file editor + diff viewer
  assert.ok(ctx.highlightCode('# a comment').includes('tok-c'), 'line comment should be tokenized');
  assert.ok(ctx.highlightCode('"a string"').includes('tok-s'), 'string should be tokenized');
  assert.ok(ctx.highlightCode('def foo():').includes('tok-k'), 'keyword should be tokenized');
  assert.ok(ctx.highlightCode('x = 42').includes('tok-n'), 'number should be tokenized');
  assert.strictEqual(ctx.highlightCode('a < b'), 'a &lt; b',
    'raw < must still be HTML-escaped even with no token match');
  assert.ok(!ctx.highlightCode('plain text only').includes('<span'), 'plain text needs no spans');

  // --- per-run usage history (opens on the usage chip, fetched separately from /state) ---
  ctx.showUsage(true);
  await new Promise(r => setImmediate(r));   // let the loadUsageHistory() fetch/then resolve
  assert.ok(get('uhistory').innerHTML.includes('add attendance summary'),
    'the recent-runs table should show the task text for each row');
  assert.ok(get('uhistory').innerHTML.includes('Byte'), 'row should show which agent ran it');
  usageHistoryResponse = {history: []};
  ctx.loadUsageHistory();
  await new Promise(r => setImmediate(r));
  assert.ok(get('uhistory').innerHTML.includes('no runs yet'), 'an empty history must say so, not render blank');
  assert.deepStrictEqual(badToasts, [], 'the history panel raised an error toast: ' + badToasts.join(' | '));

  // --- self-git panel (the dashboard's own repo, separate from desk worktrees) ---
  assert.ok(get('selfgitchip').innerHTML.includes('main') && get('selfgitchip').innerHTML.includes('clean'),
    'a clean repo should show "clean" on the chip');
  assert.strictEqual(get('sgpull').disabled, false, 'pull is allowed on a clean tree even if "behind" is stale');
  selfGitResponse = {branch: 'dev', remote: 'git@github.com:x/y.git', dirty: ['fleet.py', 'index.html'],
    ahead: 1, behind: 0, last_commit: {hash: 'def456', subject: 'wip', when: '5m ago', author: 'saad'}};
  ctx.showSelfGit(true);
  await new Promise(r => setImmediate(r));
  assert.ok(get('selfgitchip').innerHTML.includes('2 changed'), 'dirty count should show on the chip');
  assert.strictEqual(get('sgpull').disabled, true, 'pull must be blocked while there are uncommitted changes');
  assert.strictEqual(get('sgpush').disabled, false);
  assert.ok(get('sgdirty').innerHTML.includes('fleet.py') && get('sgdirty').innerHTML.includes('index.html'),
    'both dirty files should be listed');
  assert.ok(get('sglast').textContent.includes('def456') && get('sglast').textContent.includes('wip'));
  assert.deepStrictEqual(badToasts, [], 'the self-git panel raised an error toast: ' + badToasts.join(' | '));

  // --- self-git: Project tab (read-only status + branches, no push/pull) ---
  ctx.switchSelfGitRepo('project');
  await new Promise(r => setImmediate(r));
  assert.strictEqual(get('sgpushrow').hidden, true, 'the Project tab must not offer commit/push controls');
  assert.strictEqual(get('sgbranches').hidden, false, 'the Project tab shows the branch list instead');
  assert.ok(get('sgbranch').textContent === 'saad-dev');
  assert.ok(get('sgbranchlist').innerHTML.includes('stage') && get('sgbranchlist').innerHTML.includes('usama1'),
    "all of the Project's branches should be listed");
  assert.ok(get('sgdirty').innerHTML.includes('website_rastgar/models/x.py'));
  ctx.switchSelfGitRepo('self');
  await new Promise(r => setImmediate(r));
  assert.strictEqual(get('sgpushrow').hidden, false, 'switching back to Agent Town restores the push controls');
  assert.strictEqual(get('sgbranches').hidden, true);
  assert.deepStrictEqual(badToasts, [], 'the Project tab raised an error toast: ' + badToasts.join(' | '));

  // --- activity log ---
  ctx.showActivity(true);
  await new Promise(r => setImmediate(r));
  assert.ok(get('actrows').innerHTML.includes('finished ok'), 'a normal event should render');
  assert.ok(get('actrows').innerHTML.includes('push rejected'), 'an error-level event should render too');
  assert.ok(get('actcount').textContent.includes('2'));
  eventsResponse = {events: []};
  ctx.loadActivity();
  await new Promise(r => setImmediate(r));
  assert.ok(get('actrows').innerHTML.includes('nothing yet'), 'an empty activity log must say so, not render blank');
  assert.deepStrictEqual(badToasts, [], 'the activity panel raised an error toast: ' + badToasts.join(' | '));

  // --- desktop notifications ---
  ctx.paintNotifChip();
  assert.strictEqual(get('notifchip').textContent, '🔕', 'permission not yet granted -> off icon');
  ctx.notifyTaskDone('Byte', true);
  assert.strictEqual(notificationsSpawned.length, 0, 'must not notify before permission is granted');
  get('notifchip').onclick();
  await new Promise(r => setImmediate(r));
  ctx.paintNotifChip();
  assert.strictEqual(get('notifchip').textContent, '🔔', 'granted permission -> on icon');
  ctx.notifyTaskDone('Byte', true);
  assert.strictEqual(notificationsSpawned.length, 1, 'hidden+unfocused tab should get a real notification once granted');
  assert.ok(notificationsSpawned[0].includes('Byte'));
  ctx.document.visibilityState = 'visible'; ctx.document.hasFocus = () => true;
  ctx.notifyTaskDone('Byte', true);
  assert.strictEqual(notificationsSpawned.length, 1, "already looking at the tab -> no redundant notification");
  assert.deepStrictEqual(badToasts, [], 'notifications raised an error toast: ' + badToasts.join(' | '));

  // --- create-PR button (opens the returned PR URL) ---
  ctx.pick('desk-1');
  get('prbase').value = 'main';
  get('shipmsg').value = 'PR title line\nmore body text';
  const realConfirm = ctx.confirm;
  ctx.confirm = () => true;
  try{
    await get('bpr').onclick();
    assert.ok(openedUrls.includes('https://github.com/x/y/pull/9'), 'a successful PR must open its URL');
    prResponse = {error: 'run a review on this desk first'};
    await get('bpr').onclick();
    assert.strictEqual(openedUrls.length, 1, 'a failed PR response must not open anything new');
  } finally { ctx.confirm = realConfirm; }
  assert.deepStrictEqual(badToasts.filter(t => !t.includes('review')), [],
    'unexpected error toast from the PR button flow: ' + badToasts.join(' | '));

  console.log('ui test ok — 20 polls, usage chip + history + self-git panel (both repo tabs) + activity log + desktop notifications + conflict resolver + per-hunk accept render, build() ran only once, confetti-on-done works correctly, no UI-error toasts, highlightCode ok');
})();
