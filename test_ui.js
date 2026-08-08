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
  href: '', onclick: null, onkeydown: null, remove(){}, addEventListener(){},
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

const store = {};
const ctx = {
  document: {
    getElementById: get, querySelectorAll: () => [mkEl('a')],
    documentElement: {dataset: {}}, createElement: () => mkEl('toast'),
    body: mkEl('body'), activeElement: {tagName: 'BODY', blur(){}},
    addEventListener(){}, removeEventListener(){},
  },
  getComputedStyle: () => ({getPropertyValue: () => '#fff'}),
  fetch: async () => ({ok: true, json: async () => state}),
  localStorage: {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }},
  setInterval: () => 0, setTimeout: () => 0, clearTimeout(){}, console,
  window: {open(){}}, navigator: {mediaDevices: null, clipboard: {writeText: async () => {}}},
  confirm: () => false, addEventListener(){}, removeEventListener(){},
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

  // highlightCode() - regex-based tokenizer for the file editor + diff viewer
  assert.ok(ctx.highlightCode('# a comment').includes('tok-c'), 'line comment should be tokenized');
  assert.ok(ctx.highlightCode('"a string"').includes('tok-s'), 'string should be tokenized');
  assert.ok(ctx.highlightCode('def foo():').includes('tok-k'), 'keyword should be tokenized');
  assert.ok(ctx.highlightCode('x = 42').includes('tok-n'), 'number should be tokenized');
  assert.strictEqual(ctx.highlightCode('a < b'), 'a &lt; b',
    'raw < must still be HTML-escaped even with no token match');
  assert.ok(!ctx.highlightCode('plain text only').includes('<span'), 'plain text needs no spans');

  console.log('ui test ok — 20 polls, usage chip + conflict resolver render, build() ran only once, confetti-on-done works correctly, no UI-error toasts, highlightCode ok');
})();
