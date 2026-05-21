// Phase 5 — Live-Run-Bug State-Machine Tests
// Reproduziert den Bug aus AT-11CEB21120E7799B: Backend liefert result_data
// (netto/brutto/arbeitstage) + _review_items[pending] aber kein canonical_state.
// Frontend muss daraus needs_review ableiten — NIE „Status wird geprüft".
//
// Run: node tests/test_frontend_state_machine_live_run.mjs

import fs from 'node:fs';
import vm from 'node:vm';

const SITE = '/Users/miguelschumann/Desktop/site/index.html';
const html = fs.readFileSync(SITE, 'utf8');

// Inline-Scripts extrahieren
const re = /<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g;
let scripts = '';
let m;
while ((m = re.exec(html)) !== null) scripts += m[1] + '\n';

// Window-Stub damit window._normalizeBackendState / deriveUiState assignable sind
const sandbox = {
  window: {},
  document: { getElementById: () => null, querySelectorAll: () => [] },
  console,
  localStorage: { getItem: () => null, setItem: () => null, removeItem: () => null },
  navigator: { clipboard: { writeText: () => Promise.resolve() } },
  location: { search: '', hash: '', reload: () => {} },
  setTimeout: () => 0, clearTimeout: () => {},
  setInterval: () => 0, clearInterval: () => {},
  requestAnimationFrame: () => 0,
  fetch: () => Promise.reject(new Error('no-fetch')),
  Event: class {}, CustomEvent: class {},
};
sandbox.window.document = sandbox.document;
sandbox.window.localStorage = sandbox.localStorage;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);

// Nur die State-Machine-Helfer laden (sicher), den ganzen Rest skippen.
// Wir suchen die zwei Funktionen via Regex und evaluieren sie isoliert.
function extractFn(name) {
  const reFn = new RegExp(
    `window\\.${name}\\s*=\\s*function[\\s\\S]*?^\\};`,
    'm'
  );
  const match = scripts.match(reFn);
  if (!match) throw new Error(`could not extract ${name}`);
  return match[0];
}

const normalizeFn = extractFn('_normalizeBackendState');
const deriveFn = extractFn('deriveUiState');
const canShowFn = `window.canShowPdfDownload = function(s){
  if (!s) return false;
  if (typeof s.pdf_allowed === 'boolean' && !s.pdf_allowed) return false;
  return !!s.download_url;
};`;

vm.runInContext(canShowFn + '\n' + normalizeFn + '\n' + deriveFn, sandbox);

const normalize = sandbox.window._normalizeBackendState;
const derive    = sandbox.window.deriveUiState;

let pass = 0, fail = 0;
function assert(name, cond, detail) {
  if (cond) { console.log(`  ✓ ${name}`); pass++; }
  else      { console.error(`  ✗ ${name}` + (detail ? `\n      → ${detail}` : '')); fail++; }
}

console.log('\n— Case 1: Bug-Repro AT-11CEB21120E7799B (no canonical_state, result_data + pending review)');
{
  const j = {
    download_url: '/api/download/abc',
    job_id: 'd0bdc8d7',
    token: 'AT-11CEB21120E7799B',
    result_data: {
      netto: 976.0, brutto: 52884.81, arbeitstage: 135,
      _review_items: [{ status: 'pending', type: 'unknown_marker', question: 'RB?' }],
    },
  };
  const n = normalize(j);
  assert('canonical_state derived', n.canonical_state === 'needs_review',
    `got=${n.canonical_state}`);
  assert('pdf_allowed=false (needs_review locks PDF)', n.pdf_allowed === false);
  assert('review_items top-lifted', Array.isArray(n.review_items) && n.review_items.length === 1);
  const ui = derive(n);
  assert('banner_title NOT "Status wird geprüft"', ui.banner_title !== 'Status wird geprüft',
    `got banner_title=${JSON.stringify(ui.banner_title)}`);
  assert('banner_title contains "kurze Klärung"', /kurze Klärung/.test(ui.banner_title),
    `got=${ui.banner_title}`);
  assert('show_review_chat=true', ui.show_review_chat === true);
  assert('show_final_amount=false', ui.show_final_amount === false);
}

console.log('\n— Case 2: result_data only (no review), no canonical_state → done');
{
  const j = {
    download_url: '/api/download/xyz',
    job_id: 'job1',
    result_data: { netto: 500, brutto: 30000, arbeitstage: 100, _review_items: [] },
  };
  const n = normalize(j);
  assert('canonical_state=done', n.canonical_state === 'done');
  assert('pdf_allowed=true (has download_url)', n.pdf_allowed === true);
  const ui = derive(n);
  assert('banner_title="Auswertung fertig"', ui.banner_title === 'Auswertung fertig');
  assert('show_final_amount=true', ui.show_final_amount === true);
  assert('show_pdf_download=true', ui.show_pdf_download === true);
}

console.log('\n— Case 3: empty response (no canonical_state, no result) → processing');
{
  const j = { job_id: 'job2', result_data: {} };
  const n = normalize(j);
  assert('canonical_state=processing', n.canonical_state === 'processing');
  const ui = derive(n);
  assert('banner_title="Auswertung läuft"', ui.banner_title === 'Auswertung läuft');
  assert('show_refresh_status=true', ui.show_refresh_status === true);
}

console.log('\n— Case 4: explicit fetch_error → fetch_error wins');
{
  const j = {
    fetch_error: true,
    result_data: { netto: 500, brutto: 30000, arbeitstage: 100 },
  };
  const n = normalize(j);
  assert('canonical_state=fetch_error', n.canonical_state === 'fetch_error');
  const ui = derive(n);
  assert('banner_title="Verbindung kurz unterbrochen"',
    ui.banner_title === 'Verbindung kurz unterbrochen');
}

console.log('\n— Case 5: status=failed_* → failed_retryable wins');
{
  const j = { status: 'failed_timeout', result_data: { netto: 500 } };
  const n = normalize(j);
  assert('canonical_state=failed_retryable', n.canonical_state === 'failed_retryable');
}

console.log('\n— Case 6: error field set, no canonical_state → failed_retryable');
{
  const j = { error: 'Server explodiert', result_data: {} };
  const n = normalize(j);
  assert('canonical_state=failed_retryable', n.canonical_state === 'failed_retryable');
}

console.log('\n— Case 7: top-level review_items takes priority over result_data._review_items');
{
  const j = {
    review_items: [{ status: 'pending', type: 'a' }, { status: 'pending', type: 'b' }],
    result_data: {
      netto: 500, brutto: 30000, arbeitstage: 100,
      _review_items: [{ status: 'pending', type: 'c' }],
    },
  };
  const n = normalize(j);
  assert('canonical_state=needs_review', n.canonical_state === 'needs_review');
  assert('user_message has 2 (top-level count)', /2 Punkte/.test(n.user_message),
    `got=${n.user_message}`);
}

console.log('\n— Case 8: backend-provided canonical_state is preserved (no override)');
{
  const j = {
    canonical_state: 'failed_support',
    user_title: 'Custom',
    result_data: { netto: 500, brutto: 30000, arbeitstage: 100 },
  };
  const n = normalize(j);
  assert('canonical_state=failed_support (preserved)', n.canonical_state === 'failed_support');
  assert('user_title preserved', n.user_title === 'Custom');
}

console.log('\n— Case 9: review with non-pending items (resolved/skipped) → done');
{
  const j = {
    download_url: '/dl',
    result_data: {
      netto: 500, brutto: 30000, arbeitstage: 100,
      _review_items: [{ status: 'resolved' }, { status: 'skipped' }],
    },
  };
  const n = normalize(j);
  assert('canonical_state=done (no pending)', n.canonical_state === 'done');
}

console.log('\n— Case 10: derived "unknown" no longer reachable for typical bug shape');
{
  // Genau der Shape aus dem Bug-Report — wir wollen NIEMALS unknown sehen.
  const j = {
    download_url: '/dl',
    expires: '2026-05-21',
    job_id: 'x',
    notes: [],
    result_data: { netto: 976, brutto: 52884.81, arbeitstage: 135,
      _review_items: [{ status: 'pending' }] },
    token: 'AT-X',
  };
  const ui = derive(normalize(j));
  assert('status_kind != unknown', ui.status_kind !== 'unknown',
    `got status_kind=${ui.status_kind}`);
}

console.log('\n— Case 11: review-only with no result (rare race) → needs_review preferred over processing');
{
  const j = {
    result_data: { _review_items: [{ status: 'pending' }] },
  };
  const n = normalize(j);
  assert('canonical_state=needs_review (review even without result)',
    n.canonical_state === 'needs_review');
}

console.log('\n— Case 12: null/undefined input survives');
{
  const n1 = normalize(null);
  const n2 = normalize(undefined);
  assert('normalize(null) → empty object', n1 && typeof n1 === 'object');
  assert('normalize(undefined) → empty object', n2 && typeof n2 === 'object');
}

console.log(`\n— Summary: ${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
