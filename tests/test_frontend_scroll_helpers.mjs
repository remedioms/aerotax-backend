// Phase 3 — scroll helper tests.
// Verifiziert _centerActiveCard, _scrollToActivePanel, _prefersReducedMotion,
// _userIsTyping. Verwendet einen Mini-DOM-Stub statt jsdom (kein extra dep).
// Run: node tests/test_frontend_scroll_helpers.mjs

import fs from 'node:fs';
import vm from 'node:vm';

const SITE = process.env.AEROTAX_SITE_ROOT
  ? `${process.env.AEROTAX_SITE_ROOT}/index.html`
  : [
      `${process.env.HOME}/Desktop/AeroTax/site/index.html`,
      `${process.env.HOME}/Desktop/site/index.html`,
      `${process.env.HOME}/Developer/site/index.html`,
    ].find(p => fs.existsSync(p));
if (!SITE) { console.log('SKIP: site repo not found (set AEROTAX_SITE_ROOT)'); process.exit(0); }
const html = fs.readFileSync(SITE, 'utf8');

const re = /<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g;
let scripts = '';
let m;
while ((m = re.exec(html)) !== null) scripts += m[1] + '\n';

// Helper-Funktionen via Regex extrahieren
function extract(name) {
  const reFn = new RegExp(
    `window\\.${name}\\s*=\\s*function[\\s\\S]*?^\\};`,
    'm'
  );
  const match = scripts.match(reFn);
  if (!match) throw new Error(`could not extract ${name}`);
  return match[0];
}

// DOM-Stub mit minimal benötigten APIs
function makeElement(id, opts) {
  opts = opts || {};
  const el = {
    id,
    tagName: opts.tag || 'DIV',
    isContentEditable: !!opts.editable,
    offsetHeight: opts.height || 0,
    getBoundingClientRect: () => ({
      top: opts.top || 0,
      height: opts.height || 0,
      left: 0, right: 0, bottom: opts.height || 0,
    }),
  };
  return el;
}

function makeSandbox(elements, opts) {
  opts = opts || {};
  const calls = [];
  const sandbox = {
    window: {
      scrollY: 0,
      pageYOffset: 0,
      innerHeight: opts.vh || 800,
      matchMedia: (q) => ({ matches: !!opts.reducedMotion }),
      scrollTo: (x) => calls.push(x),
    },
    document: {
      activeElement: opts.activeElement || null,
      documentElement: { clientHeight: opts.vh || 800 },
      getElementById: (id) => elements[id] || null,
      querySelector: (sel) => {
        if (sel === '.panel.active') return elements['__activePanel'] || null;
        return null;
      },
    },
    requestAnimationFrame: (fn) => fn(),
    console,
    Date: { now: () => (opts._now || (opts._now = 1_000_000)) },
  };
  sandbox.window.document = sandbox.document;
  sandbox.window.requestAnimationFrame = sandbox.requestAnimationFrame;
  sandbox.window.scrollY = 0;
  sandbox.window.pageYOffset = 0;
  sandbox.window.innerHeight = opts.vh || 800;
  sandbox.window.matchMedia = sandbox.window.matchMedia;
  sandbox.window.scrollTo = sandbox.window.scrollTo;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  // Inject helpers
  const code = [
    extract('_prefersReducedMotion'),
    extract('_userIsTyping'),
    extract('_centerActiveCard'),
    extract('_scrollToActivePanel'),
  ].join('\n');
  vm.runInContext(code, sandbox);
  return { sandbox, calls };
}

let pass = 0, fail = 0;
function assert(name, cond, detail){
  if(cond){ console.log(`  ✓ ${name}`); pass++; }
  else { console.error(`  ✗ ${name}` + (detail ? `\n      → ${detail}` : '')); fail++; }
}

console.log('\n— Case 1: centerActiveCard centers element below viewport top');
{
  const result = makeElement('p-result', { top: 1200, height: 600 });
  const { sandbox, calls } = makeSandbox({ 'p-result': result }, { vh: 800 });
  sandbox.window._centerActiveCard(result, { force: true });
  assert('scrollTo called once', calls.length === 1, `calls=${calls.length}`);
  if(calls.length){
    const { top, behavior } = calls[0];
    // expected: docTop=1200, vh=800, elH=600 → target = 1200 - (800-600)/2 = 1100
    assert('target ~1100', Math.abs(top - 1100) < 1, `got=${top}`);
    assert('behavior=smooth', behavior === 'smooth');
  }
}

console.log('\n— Case 2: prefers-reduced-motion → behavior:auto');
{
  const result = makeElement('p-result', { top: 1200, height: 600 });
  const { sandbox, calls } = makeSandbox({ 'p-result': result }, {
    vh: 800, reducedMotion: true,
  });
  sandbox.window._centerActiveCard(result, { force: true });
  assert('scrollTo called', calls.length === 1);
  if(calls.length) assert('behavior=auto', calls[0].behavior === 'auto', `got=${calls[0].behavior}`);
}

console.log('\n— Case 3: skip scroll when user is typing in input');
{
  const result = makeElement('p-result', { top: 1200, height: 600 });
  const inputEl = { tagName: 'INPUT', isContentEditable: false };
  const { sandbox, calls } = makeSandbox({ 'p-result': result }, {
    vh: 800, activeElement: inputEl,
  });
  sandbox.window._centerActiveCard(result, {}); // force=false
  assert('no scroll when typing', calls.length === 0, `calls=${calls.length}`);
}

console.log('\n— Case 4: force=true overrides typing-skip');
{
  const result = makeElement('p-result', { top: 1200, height: 600 });
  const inputEl = { tagName: 'TEXTAREA', isContentEditable: false };
  const { sandbox, calls } = makeSandbox({ 'p-result': result }, {
    vh: 800, activeElement: inputEl,
  });
  sandbox.window._centerActiveCard(result, { force: true });
  assert('scroll happens despite typing when force=true', calls.length === 1);
}

console.log('\n— Case 5: contentEditable counts as typing');
{
  const result = makeElement('p-result', { top: 1200, height: 600 });
  const ed = { tagName: 'DIV', isContentEditable: true };
  const { sandbox, calls } = makeSandbox({ 'p-result': result }, {
    vh: 800, activeElement: ed,
  });
  sandbox.window._centerActiveCard(result, {});
  assert('no scroll when contentEditable focused', calls.length === 0);
}

console.log('\n— Case 6: large element (>0.9 vh) uses header offset');
{
  // elH=900 > 0.9*vh=720 → target = docTop - vh*offsetRatio
  const result = makeElement('p-result', { top: 1200, height: 900 });
  const { sandbox, calls } = makeSandbox({ 'p-result': result }, { vh: 800 });
  sandbox.window._centerActiveCard(result, { force: true, headerOffsetRatio: 0.06 });
  assert('scrollTo called', calls.length === 1);
  if(calls.length){
    // expected target = 1200 - 800*0.06 = 1152
    assert('header-offset path used', Math.abs(calls[0].top - 1152) < 1,
      `got=${calls[0].top}`);
  }
}

console.log('\n— Case 7: scrollToActivePanel(result) targets p-result');
{
  const result = makeElement('p-result', { top: 500, height: 300 });
  const { sandbox, calls } = makeSandbox({ 'p-result': result }, { vh: 800 });
  sandbox.window._scrollToActivePanel('result');
  assert('scrollTo called', calls.length === 1);
}

console.log('\n— Case 8: scrollToActivePanel(tool) targets #tool');
{
  const tool = makeElement('tool', { top: 100, height: 400 });
  const { sandbox, calls } = makeSandbox({ tool }, { vh: 800 });
  sandbox.window._scrollToActivePanel('tool');
  assert('scrollTo called', calls.length === 1);
}

console.log('\n— Case 9: scrollToActivePanel with unknown reason falls back to active panel');
{
  const active = makeElement('p2', { top: 700, height: 300 });
  const { sandbox, calls } = makeSandbox({
    '__activePanel': active,
  }, { vh: 800 });
  sandbox.window._scrollToActivePanel('step');
  assert('scrollTo called for step', calls.length === 1);
}

console.log('\n— Case 10: target never negative');
{
  const tiny = makeElement('p-result', { top: 10, height: 50 });
  const { sandbox, calls } = makeSandbox({ 'p-result': tiny }, { vh: 800 });
  sandbox.window._centerActiveCard(tiny, { force: true });
  assert('scrollTo called', calls.length === 1);
  if(calls.length) assert('top >= 0', calls[0].top >= 0, `got=${calls[0].top}`);
}

console.log(`\n— Summary: ${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
