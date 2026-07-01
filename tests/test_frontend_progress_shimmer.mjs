// Phase 4B — Progress shimmer + escalation static-audit.
// Da der Heartbeat-Loop async läuft (setInterval/setTimeout) ist ein voller
// Run-Test in Node ohne jsdom unrealistisch. Stattdessen verifizieren wir
// static, dass die Marker und Eskalations-Strings im Bundle vorhanden sind und
// dass keine Regression das Indeterminate-CSS-Pattern entfernt.
// Run: node tests/test_frontend_progress_shimmer.mjs

import fs from 'node:fs';

const SITE = process.env.AEROTAX_SITE_ROOT
  ? `${process.env.AEROTAX_SITE_ROOT}/index.html`
  : [
      `${process.env.HOME}/Desktop/AeroTax/site/index.html`,
      `${process.env.HOME}/Desktop/site/index.html`,
      `${process.env.HOME}/Developer/site/index.html`,
    ].find(p => fs.existsSync(p));
if (!SITE) { console.log('SKIP: site repo not found (set AEROTAX_SITE_ROOT)'); process.exit(0); }
const html = fs.readFileSync(SITE, 'utf8');

let pass = 0, fail = 0;
function check(name, cond, detail){
  if(cond){ console.log(`  ✓ ${name}`); pass++; }
  else { console.error(`  ✗ ${name}` + (detail ? `\n      → ${detail}` : '')); fail++; }
}

console.log('\n— Progress 92%-stuck Fix — Static Audit');

check('CSS class .pf-indeterminate defined',
  /#pf\.pf-indeterminate\b/.test(html));

check('Shimmer keyframes defined',
  /@keyframes\s+pfShimmer/.test(html));

check('pf-indeterminate applied in heartbeat loop',
  /classList\.add\(\s*['"]pf-indeterminate['"]\s*\)/.test(html));

check('Shimmer cleared on done (100%)',
  /pfEl\.classList\.remove\(\s*['"]pf-indeterminate['"]\s*\)/.test(html)
  || /pfDone\.classList\.remove\(\s*['"]pf-indeterminate['"]\s*\)/.test(html));

check('Heartbeat tracks elapsed time for escalation',
  /heartbeatStart/.test(html));

check('Escalation level 1 message (>90s) present',
  /kann bei vielen Dokumenten ein paar Minuten dauern/.test(html));

check('Escalation level 2 message (>5min) present',
  /Du kannst mit deinem Zugangscode später zurückkommen/.test(html));

check('Reduced-motion handled for shimmer',
  /prefers-reduced-motion[\s\S]{0,400}pf-indeterminate/.test(html)
  || /pf-indeterminate[\s\S]{0,400}animation:\s*none/.test(html));

check('Bar still capped at 92% (no false 100% pre-done)',
  /Math\.min\(92,\s*cur2\s*\+\s*inc\)/.test(html));

check('Bar jumps to 100% only when calculate done',
  /pfEl\.style\.width\s*=\s*['"]100%['"]/.test(html));

check('Shimmer threshold >=91.5%',
  /parseFloat\(pfH\.style\.width\|\|0\)\s*>=\s*91\.5/.test(html));

console.log('\n— Glass-Card-Transition Static Audit');

check('.panel-entering CSS class defined',
  /\.panel\.panel-entering\b/.test(html));

check('panelGlassIn keyframes defined',
  /@keyframes\s+panelGlassIn/.test(html));

check('panel-entering applied in go()',
  /classList\.add\(\s*['"]panel-entering['"]\s*\)/.test(html));

check('panel-entering cleanup via animationend listener',
  /animationend[\s\S]{0,200}panel-entering/.test(html));

check('Glass animation reduced-motion respected',
  /prefers-reduced-motion[\s\S]{0,300}panel-entering[\s\S]{0,100}animation:\s*none/.test(html));

console.log('\n— Scroll Static Audit');

check('_scrollToActivePanel defined',
  /window\._scrollToActivePanel\s*=\s*function/.test(html));

check('_centerActiveCard defined',
  /window\._centerActiveCard\s*=\s*function/.test(html));

check('_prefersReducedMotion defined',
  /window\._prefersReducedMotion\s*=\s*function/.test(html));

check('_userIsTyping defined',
  /window\._userIsTyping\s*=\s*function/.test(html));

check('render() no longer uses window.scrollTo(0,0) in done-path',
  !/render\(d\)\{[\s\S]{0,8000}window\.scrollTo\(0,0\)/.test(html));

check('showCalculationError uses scrollToActivePanel',
  /_scrollToActivePanel\(\s*['"]error['"]/.test(html));

check('go() panel-switch uses _centerActiveCard',
  /_centerActiveCard\(\s*_activeStepPanel/.test(html));

console.log(`\n— Summary: ${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
