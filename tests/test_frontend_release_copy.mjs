// Final UI/Copy Release — Static Audit
// Stellt sicher, dass Hero/USP/Trust/Download/Source-Legend genau das tragen
// was im Release-Prompt definiert wurde, und dass keine Angst-/Klein-Verkauf-
// Copy mehr im Hauptflow steht.
//
// Run: node tests/test_frontend_release_copy.mjs

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

let pass = 0;
let fail = 0;
function check(name, ok, detail) {
  if (ok) { console.log('  ✓ ' + name); pass++; }
  else    { console.log('  ✗ ' + name + (detail ? ' — ' + detail : '')); fail++; }
}

// ─── Hero ───────────────────────────────────────────────────────────────────
console.log('\n[hero]');
check('hero_contains_aus_dienstplan_und_spesen',
  html.includes('Aus Dienstplan und Spesen wird dein Steuer-Überblick'));
check('hero_contains_dienstplan_rein_ueberblick_raus',
  html.includes('Dienstplan rein. Überblick raus.'));
check('hero_contains_fuer_flugpersonal_gemacht',
  html.includes('Für Flugpersonal gemacht'));
check('hero_mentions_streckeneinsatz_in_subline_or_usp',
  html.includes('Streckeneinsatz'));
// Homepage Master 2026-05-21: Hero komplett neu. „Du fliegst..." entfernt.
// H1 = „Dienstplan rein. Überblick raus." + Sub erklärt CAS/SE/LSB → PDF.
check('hero_h1_is_dienstplan_rein_ueberblick_raus',
  /<div class="htag">Dienstplan rein\. Überblick raus\.<\/div>/.test(html));
check('hero_does_not_contain_du_fliegst_wir_rechnen',
  !html.includes('Du fliegst. Wir rechnen. Du trägst ein.') &&
  !/<p class="hsub"[^>]*>Du fliegst/.test(html));
check('hero_mentions_cas_streckeneinsatz_lohnsteuerdaten',
  (function(){
    const m = html.match(/<p class="hsub"[^>]*>([^<]+)<\/p>/);
    return m && /CAS/.test(m[1]) && /Streckeneinsatz/.test(m[1]) && /Lohnsteuerdaten/.test(m[1]);
  })());
check('hero_mentions_pdf_auswertung',
  (function(){
    const m = html.match(/<p class="hsub"[^>]*>([^<]+)<\/p>/);
    return m && /PDF-Auswertung/.test(m[1]);
  })());
check('hero_supporting_claim_fuer_flugpersonal',
  /Für Flugpersonal gemacht\. Für die Steuer vorbereitet\./.test(html));
check('hero_no_long_checkmark_chain',
  (function(){
    const heroStart = html.indexOf('<div class="hero">');
    const heroEnd = html.indexOf('<!-- WIE AEROTAX ARBEITET', heroStart);
    if (heroStart < 0 || heroEnd < 0) return false;
    const hero = html.slice(heroStart, heroEnd);
    const checkmarks = (hero.match(/✓/g) || []).length;
    return checkmarks < 3;
  })());

// ─── USP-Block ──────────────────────────────────────────────────────────────
console.log('\n[usp]');
// Iteration 2026-05-21: USP + HOW zu ONE Section zusammengelegt.
// User-Request: „aus 2 einen element nur machen". id="how" trägt jetzt beide
// Rollen: Was-macht-es + In-3-Schritten kombiniert in einer Section.
check('combined_section_present',
  html.includes('id="how"') && /Wie AeroTAX arbeitet/.test(html));
// Homepage Master 2026-05-21: USP von 5 Cards auf 3 reduziert, keine
// langen Texte mehr — Crew-Details (Marker/Standby/Hotelnächte) gehören in FAQ.
check('combined_section_has_three_cards',
  (function(){
    const start = html.indexOf('id="how"');
    const end = html.indexOf('<!-- TOOL', start);
    if (start < 0 || end < 0) return false;
    const block = html.slice(start, end);
    const cards = (block.match(/<div class="hc reveal">/g) || []).length;
    return cards === 3;
  })());
check('combined_section_no_giant_card_text',
  (function(){
    const start = html.indexOf('id="how"');
    const end = html.indexOf('<!-- TOOL', start);
    if (start < 0 || end < 0) return false;
    const block = html.slice(start, end);
    return !block.includes('Marker, Standby und Tour-Kontext') &&
           !block.includes('Doppelansatz, kein Verlust');
  })());
check('only_one_marketing_snap_section_not_two',
  (function(){
    // Should be exactly 1 .snap-section in the main flow (Hero is .hero, not snap).
    const snapCount = (html.match(/<div class="snap-section">/g) || []).length;
    return snapCount === 1;
  })());
check('combined_section_uses_du_aerotax_du_pattern',
  /01 · Du[\s\S]{0,1500}02 · AeroTAX[\s\S]{0,1500}03 · Du/.test(html));
check('combined_section_mentions_cas',
  /Unterlagen hochladen|Dienstplan\/CAS/.test(html));
check('combined_section_mentions_streckeneinsatz',
  html.includes('Streckeneinsatz'));
check('combined_section_mentions_z77',
  /Z77\/stfrei/.test(html));
check('combined_section_mentions_pdf_eintragen',
  /PDF eintragen[\s\S]{0,200}Steuererklärung/.test(html));

// ─── Trust-Badges ───────────────────────────────────────────────────────────
console.log('\n[trust badges]');
// Homepage Master 2026-05-21: Trust-Badge-Chain im Hero entfernt (zu textlastig).
// CAS/Dienstplan-Hinweis steckt jetzt in der Hero-Subline + USP-Card.
check('hero_subline_carries_trust_signals',
  /<p class="hsub"[^>]*>([^<]*CAS[^<]*Streckeneinsatz[^<]*Lohnsteuerdaten[^<]*PDF-Auswertung)/.test(html));
// Homepage Master 2026-05-21: lange Häkchen-Kette aus dem Hero entfernt.
// Trust signals leben jetzt in Result-Panel/Datenschutz/FAQ statt im Hero.
check('hero_keeps_price_and_supporting_claim',
  /Einmalig 19,99 €\s*·\s*Kein Abo\s*·\s*PDF mit Quellen/.test(html));
check('badges_streckeneinsatz_abgleich_still_referenced_in_usp',
  html.includes('Spesen abgleichen'));
check('badges_pdf_quellen_in_hero_price_line',
  html.includes('PDF mit Quellen'));
check('badges_z77_referenced_in_usp',
  /Z77\/stfrei/.test(html));

// ─── Disclaimer / Legal nicht im Hero ───────────────────────────────────────
console.log('\n[legal placement]');
// Hero-zone = vom Hero-Wrapper bis zum nächsten "<!-- HOW -->" Marker
const heroStart = html.indexOf('<div class="hero">');
const heroEnd = html.indexOf('<!-- HOW -->', heroStart);
const heroBlock = heroStart >= 0 && heroEnd > heroStart ? html.slice(heroStart, heroEnd) : '';
check('no_keine_steuerberatung_in_hero',
  !heroBlock.toLowerCase().includes('keine steuerberatung'));
check('no_haftungsausschluss_in_hero',
  !heroBlock.toLowerCase().includes('haftungsausschluss'));
check('no_klar_strukturierte_vorbereitung_anywhere',
  !html.includes('Klar strukturierte Vorbereitung'));
check('no_werbungskosten_auswertung_fuer_flugpersonal',
  !html.includes('Werbungskosten-Auswertung für Flugpersonal'));

// Download/Result-Bereich
const resStart = html.indexOf('id="p-result"');
const resEnd   = html.indexOf('id="dl-btn-row"', resStart);
const resBlock = resStart >= 0 && resEnd > resStart ? html.slice(resStart, resEnd) : '';
check('no_keine_steuerberatung_in_download_area',
  !resBlock.toLowerCase().includes('keine steuerberatung'));
check('no_haftungsausschluss_in_download_area',
  !resBlock.toLowerCase().includes('haftungsausschluss'));
check('no_warnung_visible_in_download_area',
  !resBlock.includes('Warnung'));

// Legal-Hint soll in Footer oder FAQ existieren (rechtliche Absicherung bleibt)
const footerStart = html.indexOf('<footer>');
const footerEnd   = html.indexOf('</footer>');
const footerBlock = footerStart >= 0 && footerEnd > footerStart ? html.slice(footerStart, footerEnd) : '';
const faqHasLegal = /Aero(.|\n){0,120}keine Steuerberatung/.test(html);
const footerHasLegal = /eigenverantwortlich|Steuerberatung/.test(footerBlock);
check('legal_hint_exists_footer_or_faq',
  faqHasLegal || footerHasLegal);

// Keine Garantie-Claims
console.log('\n[no tax-guarantee claims]');
check('no_100_percent_guarantee',
  !/100\s*%\s*sicher|garantiert\s*korrekt|finanzamt-sicher|steuerberater-sicher/i.test(html));
check('no_specific_percentage_guarantee',
  !/(9[0-9]\s*%\s*sicher|9[0-9]\s*%\s*Treffer)/i.test(html));

// ─── Status / Progress / Review Copy ───────────────────────────────────────
console.log('\n[status & review copy]');
// "Status wird geprüft" darf NIE als sichtbarer Banner-Wert mehr gesetzt werden.
// (Kommentare in JS sind erlaubt, weil sie nicht zum User durchschlagen.)
// Active assignment only (not comments). Real assignments use `out.banner_title = '…'`;
// the JS comments in this file contain the literal in their text without `out.`.
check('no_status_wird_geprueft_as_banner_value',
  !/out\.banner_title\s*=\s*['"]Status wird geprüft['"]/.test(html));
check('banner_title_speaks_in_active_voice',
  html.includes('Wir lesen deine Unterlagen') ||
  html.includes('Wir gleichen Dienstplan und Spesen ab') ||
  html.includes('Wir berechnen deine Werbungskosten'));
check('review_copy_uses_kurze_klaerung',
  html.includes('kurze Klärung') || html.includes('Kurze Klärung'));
check('no_angst_review_label_in_active_card',
  !/<div[^>]*review-card[^>]*>[^<]*Fehler/.test(html));

// ─── Source-Legende user-freundlich ────────────────────────────────────────
console.log('\n[source legend]');
check('source_legend_user_friendly_header',
  html.includes('Woher kommen die Werte?'));
check('source_legend_uses_chips_not_source_type',
  !html.includes('source_type=mixed') && !html.includes('source_type=document'));
check('source_legend_has_dienstplan_chip',
  /Dienstplan\s*\/\s*CAS/.test(html));
check('source_legend_has_streckeneinsatz_chip',
  html.includes('Streckeneinsatz'));
check('source_legend_has_lohnsteuerdaten_chip',
  html.includes('Lohnsteuerdaten'));
check('source_legend_has_bmf_pauschalen_chip',
  html.includes('BMF-Pauschalen'));
check('source_legend_has_user_input_chip',
  html.includes('Deine Angabe *'));
check('source_legend_explains_asterisk',
  /Mit\s*<strong[^>]*>\*<\/strong>\s*markierte Werte stammen aus deiner Eingabe/.test(html));

// ─── Download-Copy clean ────────────────────────────────────────────────────
console.log('\n[download copy]');
check('download_title_positive',
  html.includes('Dein Steuer-Überblick ist fertig') ||
  html.includes('Deine Auswertung ist bereit'));
check('download_subtext_positive',
  /berechneten Werbungskosten.{0,80}Spesen-Abgleich.{0,80}Quellen|vorbereitet zum Eintragen/.test(html));
// Homepage Master 2026-05-21: Debug-Copy „Falls die Chat-Bubble nicht lädt"
// entfernt — klang nach Bug.
check('no_falls_chat_bubble_nicht_laedt_debug_copy',
  !html.includes('Falls die Chat-Bubble nicht lädt'));
check('dl_btn_row_positive_copy',
  html.includes('Deine Auswertung ist bereit.') &&
  /24 Stunden über deinen Code/.test(html));
check('dl_btn_row_uses_glass_system',
  /id="dl-btn-row"[\s\S]{0,400}backdrop-filter:var\(--glass-blur-soft\)/.test(html));
check('dl_btn_hint_uses_nicht_bestaetigt_phrasing',
  /Nicht bestätigte Punkte werden im PDF entsprechend gekennzeichnet/.test(html));

// ─── 3-Step Flow Copy Match ────────────────────────────────────────────────
console.log('\n[3-step flow copy]');
// Headline replaced from „Hochladen. Ergänzen. Fertig." → „Aus Unterlagen wird…"
check('combined_section_uses_aus_unterlagen_headline',
  /<p class="stit[^"]*">Aus Unterlagen wird dein Steuer-Überblick\.<\/p>/.test(html));
check('combined_step1_unterlagen_hochladen',
  /01 · Du[\s\S]{0,400}Unterlagen hochladen/.test(html));
check('combined_step2_aerotax_auswertet',
  /02 · AeroTAX[\s\S]{0,400}Auswertet/.test(html));
check('combined_step3_pdf_eintragen',
  /03 · Du[\s\S]{0,400}PDF eintragen/.test(html));
check('no_steuersoftware_uebertragen_step',
  !html.includes('In Steuersoftware übertragen'));
check('no_drei_schritte_vollautomatisch',
  !html.includes('Drei Schritte. Vollautomatisch.'));

// ─── Datenschutz/Provider ──────────────────────────────────────────────────
console.log('\n[datenschutz provider cleanup]');
check('privacy_mentions_google_cloud_run_not_render',
  /<li><strong>Google Cloud Run<\/strong>/.test(html));
check('no_render_com_in_privacy_modal',
  !/<li><strong>Render\.com<\/strong>/.test(html));
check('privacy_mentions_eu_region_frankfurt',
  /europe-west3 Frankfurt/.test(html));
check('pdf_button_label_clean',
  html.includes('⬇ PDF herunterladen'));

// ─── Render-Reste sind weg ─────────────────────────────────────────────────
console.log('\n[infra cleanup]');
check('no_render_fallback_in_frontend', !html.includes('RENDER_FALLBACK'));
check('no_onrender_url_in_frontend', !html.includes('onrender.com'));
check('cloud_run_only', html.includes('CLOUD_RUN_PROD'));

// ─── UX feedback fixes ──────────────────────────────────────────────────────
console.log('\n[ux feedback fixes]');
// CAS-Hint: „mit Uhrzeiten" war kryptisch — neue Hint nennt Start-/Endzeit explizit.
check('cas_hint_explains_uhrzeiten',
  html.includes('Start-/Endzeit der Dienste') ||
  html.includes('mit Start- und Endzeit'));
check('cas_hint_no_cryptic_mit_uhrzeiten',
  !html.includes('Alle 12 Monate — mit Uhrzeiten'));

// .bo button (Zurück) should have same font-size/weight as .bn (Weiter)
const boBlock = (html.match(/\.bo\{[^}]+\}/) || [''])[0];
const bnBlock = (html.match(/\.pact \.bn\{[^}]+\}/) || [''])[0];
check('zurueck_button_font_size_15px_like_weiter',
  /font-size:\s*15px/.test(boBlock) && /font-size:\s*15px/.test(bnBlock));
check('zurueck_button_font_weight_700_like_weiter',
  /font-weight:\s*700/.test(boBlock) && /font-weight:\s*700/.test(bnBlock));

// Optionale Belege: giant explainer should NOT appear ABOVE the upload cards
// (Cards = the <details class="opt-section"> blocks)
const optStart = html.indexOf('id="opt-upload-fields"');
const firstOptDetails = html.indexOf('<details class="opt-section"', optStart);
const introSlice = optStart >= 0 && firstOptDetails > optStart ? html.slice(optStart, firstOptDetails) : '';
// Phase 4 final: no category cards at all → no „intro above cards" notion
// anymore. Dropzone IS the entire optional surface.
check('opt_no_long_explainer_anywhere_in_optional_section',
  !html.includes('Ein PDF statt Beleg-Chaos') &&
  !html.includes('Crew-Posten als Checkliste'));
// Phase 4 final: „Was bringt das?"-Card komplett entfernt.
// Optional-Section ist jetzt nur Toggle + Dropzone + Summary.
check('opt_explainer_card_completely_removed',
  !/Was bringt das\?[\s\S]{0,200}Ein PDF statt Beleg-Chaos/.test(html));

// dl-btn-row (PDF download fallback at bottom of result) must be CENTERED
const dlMatch = html.match(/<div id="dl-btn-row"[^>]*>/);
const dlAttr  = dlMatch ? dlMatch[0] : '';
check('pdf_download_bottom_card_centered',
  /text-align:\s*center/.test(dlAttr));
const dlInnerMatch = html.match(/<div id="dl-btn-row"[^>]*>\s*<div[^>]*>/);
const dlInner = dlInnerMatch ? dlInnerMatch[0] : '';
check('pdf_download_inner_card_has_margin_auto',
  /margin:\s*0\s*auto/.test(dlInner));

// ─── Emil Kowalski polish pass ─────────────────────────────────────────────
console.log('\n[emil polish]');
check('easing_variables_defined_in_root',
  /--ease-out:\s*cubic-bezier\(0\.23,\s*1,\s*0\.32,\s*1\)/.test(html));
check('timing_variables_defined_in_root',
  /--t-press:\s*\d+ms/.test(html) && /--t-pop:\s*\d+ms/.test(html));
check('no_transition_all_in_authored_styles',
  !/transition:\s*all\b/.test(html.replace(/<script[\s\S]*?<\/script>/g, '')));
check('primary_cta_bth_has_active_scale',
  /\.bth:active\{\s*transform:\s*scale\(\.?9\d\)/.test(html));
check('primary_cta_btg_has_active_scale',
  /\.btg:active\{\s*transform:\s*scale\(\.?9\d\)/.test(html));
check('pdf_dlb_has_active_scale',
  /\.dlb:active\{\s*transform:\s*scale\(\.?9\d\)/.test(html));
check('zurueck_bo_has_active_scale',
  /\.bo:active\{\s*transform:\s*scale\(\.?9\d\)/.test(html));
check('weiter_bn_has_active_scale',
  /\.pact\s*\.bn:active\{\s*transform:\s*scale\(\.?9\d\)/.test(html));
check('hover_gated_for_touch_devices',
  /@media\s*\(hover:\s*hover\)\s*and\s*\(pointer:\s*fine\)/.test(html));
check('no_scale_zero_entry_keyframe',
  !/from\s*\{\s*transform:\s*scale\(0\)\s*;?\s*\}/.test(html));
check('popIn_starts_from_six_not_zero',
  /popIn\b[\s\S]{0,200}scale\(\.6\)/.test(html));
// Phase: Scroll-Reveal via IntersectionObserver statt auto-fire keyframe.
// Stagger jetzt über transition-delay (löst nur wenn Card in Viewport scrollt).
check('usp_cards_have_scroll_reveal_stagger',
  /\.hc\.reveal:nth-child\(2\)\.is-visible\{\s*transition-delay:\s*120ms/.test(html));
check('intersection_observer_wired_for_reveals',
  /new IntersectionObserver/.test(html) &&
  /querySelectorAll\('\.hc\.reveal,\s*\.scroll-reveal'\)/.test(html));
// Iteration 2026-05-21: 3s-Fallback ENTFERNT — fired blind alle is-visible
// auf Hero-Page wenn User noch nicht runter-scrollt. Stattdessen:
// - rAF-Check für initially-in-viewport Elements (Hero-Items)
// - prefers-reduced-motion respektiert (alle sofort sichtbar)
// - kein No-Scroll-Fallback mehr
check('no_blanket_3s_fallback_that_kills_scroll_animation',
  !/setTimeout\(function\(\)\{[\s\S]{0,200}\.reveal:not\(\.is-visible\)[\s\S]{0,200}is-visible[\s\S]{0,200}3000/.test(html));
check('reveal_threshold_increased_to_35_percent',
  /threshold:\s*0\.35/.test(html));
check('reveal_rootMargin_negative_bottom_15_percent',
  /rootMargin:\s*['"]0px 0px -15% 0px['"]/.test(html));
check('reveal_initial_viewport_check_via_raf',
  /requestAnimationFrame\(function\(\)[\s\S]{0,400}getBoundingClientRect/.test(html));
check('reveal_respects_prefers_reduced_motion_via_matchmedia',
  /matchMedia\('\(prefers-reduced-motion: reduce\)'\)\.matches[\s\S]{0,300}\.hc\.reveal, \.scroll-reveal/.test(html));
// Emil: 30-80ms per step. With 5 cards, last delay = 4×50ms = 200ms.
// Assert: 150ms exists (=3 steps × 50ms), and no per-step jump leaves a gap
// over 80ms (i.e. no two consecutive delays differ by more than 80ms).
// Iteration 2026-05-21: stagger 120ms per step (war 80ms). User wollte
// stärker sichtbare scroll-animation. Max-per-step bumped to 150.
check('stagger_within_per_step_limit',
  (function(){
    const delays = [...html.matchAll(/\.hc\.reveal:nth-child\(\d\)\.is-visible\{\s*transition-delay:\s*(\d+)ms/g)]
      .map(m => parseInt(m[1], 10)).sort((a,b) => a-b);
    if(delays.length < 2) return false;
    for(let i = 1; i < delays.length; i++){
      if((delays[i] - delays[i-1]) > 150) return false;
    }
    return true;
  })());
check('reduced_motion_respected_in_stagger',
  /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]{0,400}\.hc\.reveal[\s\S]{0,100}opacity:1/.test(html));
// User-Request: equal-height + centered cards via flex+grid.
check('hgrid_uses_grid_auto_rows_1fr_for_equal_height',
  /\.hgrid\{[\s\S]{0,200}grid-auto-rows:1fr/.test(html));
check('hc_uses_flex_column_centered',
  /\.hc\{[\s\S]{0,800}display:flex;[\s\S]{0,200}flex-direction:column;[\s\S]{0,200}align-items:center;[\s\S]{0,200}text-align:center/.test(html));
check('hc_height_100_percent_for_grid_fill',
  /\.hc\{[\s\S]{0,1000}height:100%/.test(html));
check('scroll_reveal_utility_class_defined',
  /\.scroll-reveal\{[\s\S]{0,200}opacity:0/.test(html));

// User-Request 2026-05-21: jede Marketing-Section füllt 1 Viewport, Content
// vertikal zentriert. „alleine mittig, dann scrollen und animation".
check('hero_uses_min_height_100vh_centered',
  /\.hero\{[\s\S]{0,400}min-height:100vh[\s\S]{0,200}justify-content:center/.test(html));
check('snap_section_utility_defined',
  /\.snap-section\{[\s\S]{0,200}min-height:100vh[\s\S]{0,200}justify-content:center/.test(html));
check('combined_section_wrapped_in_snap',
  /<div class="snap-section"><div class="wrap"><div class="how" id="how">/.test(html));
check('snap_section_fallback_for_short_viewports',
  /@media\(max-height:680px\)\{\.snap-section\{min-height:auto/.test(html));

// ─── Liquid Glass material system ──────────────────────────────────────────
console.log('\n[liquid glass]');
check('glass_bg_variable_defined',
  /--glass-bg:\s*rgba\(255,255,255,\.\d+\)/.test(html));
check('glass_blur_variable_defined',
  /--glass-blur:\s*blur\(\d+px\)\s*saturate\(\d+%\)/.test(html));
check('glass_specular_highlight_defined',
  /--glass-spec:[\s\S]{0,200}inset\s+0\s+1px\s+0\s+rgba\(255,255,255,\.\d+\)/.test(html));
check('lg_surface_utility_class_present',
  /\.lg-surface\{/.test(html));
check('lg_sheen_utility_class_present',
  /\.lg-sheen\{/.test(html));
check('lg_sheen_uses_diagonal_125deg',
  /linear-gradient\(125deg,[\s\S]{0,80}transparent\s+35%/.test(html));
check('lg_sheen_hover_gated_for_touch',
  /\.lg-sheen:hover::before/.test(html) &&
  /@media\s*\(hover:\s*hover\)\s*and\s*\(pointer:\s*fine\)/.test(html));
check('lg_sheen_respects_reduced_motion',
  /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]{0,200}\.lg-sheen::before\{\s*display:\s*none/.test(html));
check('req_card_uses_glass_variables',
  /\.req-card\{[\s\S]{0,400}var\(--glass-bg\)[\s\S]{0,400}var\(--glass-blur\)/.test(html));
check('req_card_has_diagonal_sheen',
  /\.req-card::before\{[\s\S]{0,200}linear-gradient\(125deg/.test(html));
check('req_card_has_active_scale',
  /\.req-card:active\{\s*transform:\s*scale\(\.985\)/.test(html));

// User feedback 2026-05-21 (revision): „text reicht braucht keine buble..
// sonst verliert es glass logik". Minor status messages bleiben PURE TEXT —
// kein Background, kein Border, kein Box-Shadow. Eine zweite Glass-Surface
// dort einzusetzen würde das Glass-System verwässern.
check('status_text_utility_is_pure_text_no_bubble',
  /\.status-text\{[\s\S]{0,400}background:\s*none[\s\S]{0,80}border:\s*none[\s\S]{0,80}box-shadow:\s*none/.test(html));
check('noch_fehlend_banner_uses_status_text_class',
  /err\.className\s*=\s*['"]err\s+status-text['"]/.test(html));
check('no_hardcoded_red_cssText_for_missing_docs_banner',
  !/err\.style\.cssText\s*=\s*['"]display:block;background:rgba\(220,38,38,\.15\);border:1px solid rgba\(252,165,165,\.25\)/.test(html));
check('static_err0_uses_status_text_class',
  /<div\s+class=["']err\s+status-text["']\s+id=["']err0["']/.test(html));
check('no_lg_error_glass_bubble_class',
  !/\.lg-error\{/.test(html));
check('no_lgErrorIn_animation',
  !/@keyframes\s+lgErrorIn/.test(html));
check('req_progress_stays_simple_not_glass_pill',
  !/\.req-progress\{[\s\S]{0,300}backdrop-filter:\s*var\(--glass-blur-soft\)/.test(html));

// ─── Hero cleanup (Homepage Master 2026-05-21) ─────────────────────────────
console.log('\n[hero trim]');
check('hero_drops_redundant_gradient_claim',
  !html.includes('Dienstplan rein. Überblick raus.<br>Für Flugpersonal gemacht.'));
check('hero_no_du_fliegst_anymore',
  !html.includes('Du fliegst. Wir rechnen. Du trägst ein.'));

// ─── Vorteile-Block sichtbar (nicht versteckt) ─────────────────────────────
console.log('\n[vorteile visible]');
// Vorteile-Block + Was-Bringt-Das Card sind komplett entfernt
// (User-Request: „wenn doch nur noch einer mit optional wo einfach alles rein darf?").
check('vorteile_block_completely_gone',
  !html.includes('Ein PDF statt Beleg-Chaos') &&
  !html.includes('Crew-Posten als Checkliste'));

// ─── Phase 3: 3-Step Stepper ───────────────────────────────────────────────
console.log('\n[3-step flow]');
check('three_stage_indicator_present',
  html.includes('id="stage1"') && html.includes('id="stage2"') && html.includes('id="stage3"'));
check('stage_labels_hochladen_ergaenzen_auswertung',
  /<div class="stage-label">Hochladen<\/div>/.test(html) &&
  /<div class="stage-label">Ergänzen<\/div>/.test(html) &&
  /<div class="stage-label">Auswertung<\/div>/.test(html));
check('legacy_five_tabs_hidden_via_st_legacy_wrapper',
  /class="st-legacy"\s+aria-hidden="true"/.test(html));
check('st_legacy_display_none',
  /\.st-legacy\{display:none\s*!important/.test(html));
check('update_stages_function_wired_to_goStep',
  /window\._updateStages\s*=\s*function\(n\)/.test(html) &&
  /window\._updateStages\(n\)/.test(html));
check('stage_mapping_p0_p1_to_stage1',
  /n\s*<=\s*1\s*\?\s*1/.test(html));
check('stage_mapping_p2_p3_to_stage2',
  /n\s*<=\s*3\s*\?\s*2/.test(html));

// ─── Phase 4: Optional Belege Simplification ──────────────────────────────
console.log('\n[optional belege simple]');
// Iteration 2026-05-21: Dropzone → req-card-Pattern (gleicher Look wie LSB/SE/CAS).
// User-Request: „warum einfach sagen es ist da wie bei jedem anderen upload button".
check('opt_uses_req_card_pattern_like_cas',
  /<div class="req-card" id="rc-opt"/.test(html));
check('opt_card_describes_what_belege',
  /z\. B\. Gewerkschaft, Telefon, Fortbildung, Reinigung, Versicherungen/.test(html));
check('opt_card_uses_50_limit',
  /Bis 50 Stück/.test(html));
check('opt_card_has_weitere_hinzufuegen_button',
  /opt-btns[\s\S]{0,800}Weitere hinzufügen/.test(html));
check('opt_card_has_alle_loeschen_button',
  /opt-btns[\s\S]{0,800}Alle löschen/.test(html));
// User feedback 2026-05-21: „wenn doch nur noch einer mit optional wo einfach
// alles rein darf?" — Kategorienwand + „Was bringt das?"-Card komplett entfernt.
// Dropzone ist die EINZIGE optionale Belege-UI.
check('opt_category_wall_completely_removed',
  !/Belege nach Kategorie zuordnen/.test(html) &&
  !/<details class="opt-section"/.test(html));
check('opt_was_bringt_das_card_removed',
  !/Was bringt das\?[\s\S]{0,200}Ein PDF statt Beleg-Chaos/.test(html));
check('opt_toggle_button_is_lg_pill_not_big_circle',
  /id="opt-plus-btn"[^>]*class="lg-pill"/.test(html) &&
  !/id="opt-plus-btn"[^>]*width:56px;height:56px;border-radius:50%/.test(html));

// LSB: User-Request „warum kann ich beim knopf von lohnsteuerbescheinigung den
// pdf auch nicht löschen?" — Delete-Button (`lsb-btns`) jetzt vorhanden.
check('lsb_card_has_delete_button',
  /id="lsb-btns"[\s\S]{0,400}clearUpload\('lsb'/.test(html));
check('lsb_delete_button_says_entfernen',
  /id="clear-lsb"[\s\S]{0,80}✕ Entfernen/.test(html));

// Wake-Bar: User-Request „ready for departure unterhalb weiter zur zahlung
// mittig platzieren" — wandert aus dem Form-Top in die zentrierte Zeile unter
// dem Weiter-Button. Disappearing-Behavior bleibt (8130ff).
check('wake_bar_below_weiter_button_centered',
  /<button class="bn active-step" id="p2-weiter-btn"[\s\S]{0,400}<div id="wake-bar"/.test(html));
check('wake_bar_uses_glass_system',
  /id="wake-bar"[\s\S]{0,400}backdrop-filter:var\(--glass-blur-soft\)/.test(html));

// Bug-Fix 2026-05-21: Free-Pass-Race-Condition. Stripe-`ready`-Callback hat
// nach applyPromo() den pay-btn-Text zurück auf „Jetzt für 19,99 € kaufen"
// gesetzt — User hing fest trotz gültigem Smoke-Test-Code.
check('stripe_ready_callback_respects_free_flag',
  /paymentElement\.on\('ready'[\s\S]{0,500}if\(_free === true \|\| window\._free === true\)/.test(html));
check('stripe_ready_callback_logs_skip_when_free',
  /\[stripe\] ready fired but _free=true — skip pay-btn overwrite/.test(html));
check('apply_promo_sets_free_globally',
  /_free=true[\s\S]{0,80}window\._free = true/.test(html));
check('pay_function_handles_isFree_short_circuit',
  /var isFree = _free \|\| window\._free === true[\s\S]{0,200}if\(isFree\)/.test(html));
check('uploadOptAny_handler_defined',
  /window\.uploadOptAny\s*=\s*function/.test(html));
check('uploadOptAny_enforces_50_file_limit',
  /var MAX\s*=\s*50/.test(html) && /\.slice\(0,\s*MAX\)/.test(html));
check('uploadOptAny_appends_does_not_replace',
  /ups\['opt'\]\s*=\s*\(ups\['opt'\]\s*\|\|\s*\[\]\)\.concat/.test(html));
// Receipt-Summary-Card wurde durch req-card-Pattern ersetzt (Iteration 2026-05-21
// — User-Request: gleiches Verhalten wie LSB/SE/CAS).
check('opt_card_uses_req_card_inheriting_glass',
  /<div class="req-card" id="rc-opt"/.test(html));

// ─── Phase 5: Receipt Classifier — Design doc exists ──────────────────────
console.log('\n[receipt classifier doc]');
check('receipt_classifier_design_doc_exists',
  fs.existsSync(new URL('../docs/RECEIPT_CLASSIFIER_DESIGN.md', import.meta.url).pathname));
check('receipt_classifier_doc_specifies_inclusion_rules',
  (function(){
    const doc = fs.readFileSync(new URL('../docs/RECEIPT_CLASSIFIER_DESIGN.md', import.meta.url).pathname,'utf8');
    return doc.includes('Inclusion-Regeln') &&
           doc.includes('included_in_total') &&
           doc.includes('needs_review') &&
           doc.includes('source_type');
  })());
check('receipt_classifier_doc_specifies_z77_z17_hard_constraint',
  (function(){
    const doc = fs.readFileSync(new URL('../docs/RECEIPT_CLASSIFIER_DESIGN.md', import.meta.url).pathname,'utf8');
    return doc.includes('NICHT von Z77') && doc.includes('NICHT von Z17');
  })());

// ─── Liquid Glass Pill — System-Klasse für Glass-Pills ─────────────────────
// User feedback 2026-05-21: „Dateien werden gelöscht"-Pille + andere kleine
// Glass-Pills hatten hardcoded backdrop-filter/rgba statt System-Variablen.
// `.lg-pill` zentralisiert das Muster — alle künftigen Pills nutzen die Klasse.
console.log('\n[lg-pill system]');
check('lg_pill_utility_defined',
  /\.lg-pill\{[\s\S]{0,400}backdrop-filter:\s*var\(--glass-blur-soft\)/.test(html));
check('lg_pill_uses_glass_bg_variable',
  /\.lg-pill\{[\s\S]{0,400}background:\s*var\(--glass-bg\)/.test(html));
check('lg_pill_has_specular_highlight',
  /\.lg-pill\{[\s\S]{0,500}inset\s+0\s+1px\s+0\s+rgba\(255,255,255,\.\d+\)/.test(html));
check('lg_pill_info_variant_defined',
  /\.lg-pill\.lg-pill-info\{/.test(html));
check('lg_pill_success_variant_defined',
  /\.lg-pill\.lg-pill-success\{/.test(html));
// Iteration 2026-05-21: User „will nur text ohne glass pill drum herum".
// Pill entfernt — Datenschutz-Hinweis ist jetzt schlichter inline-Text.
check('datenschutz_hint_is_plain_text_not_pill',
  !/<div class="lg-pill">\s*<span>Dateien werden/.test(html) &&
  /Dateien werden <strong[^>]*>nur zur Berechnung<\/strong> genutzt und danach sofort gelöscht/.test(html));
// „💡 Im Zweifel hochladen"-Pille war in der entfernten „Was bringt das?"-Card —
// nicht mehr vorhanden (intentional, weil Card komplett raus).
check('zweifel_hint_pill_removed_with_card',
  !/Im Zweifel hochladen — wir sortieren den Beleg/.test(html));
check('js_status_pill_uses_lg_pill_class',
  /statusEl\.innerHTML\s*=\s*'<div class="lg-pill"/.test(html));
check('js_success_pill_uses_lg_pill_success_class',
  /<div class="lg-pill lg-pill-success"/.test(html));

// User-Request 2026-05-21: ganze AeroTAX-Nennung in Montserrat (Logo-Font),
// nicht nur TAX-Teil mit Gradient. Auto-Wrapper baut nun:
// <span class="aerotax-wordmark">Aero<span class="t">TAX</span></span>
console.log('\n[wordmark wrap]');
check('aerotax_wordmark_class_defined',
  /\.aerotax-wordmark\{[\s\S]{0,200}font-family:\s*'Montserrat'/.test(html));
check('auto_wrapper_uses_wordmark_class',
  /mark\.className\s*=\s*['"]aerotax-wordmark['"]/.test(html));
check('hero_uses_simple_centered_hin_no_min_height_hack',
  !/\.hin\{[\s\S]{0,800}min-height:\s*calc\(100vh/.test(html));
check('scroll_reveal_blur_enhanced',
  /\.scroll-reveal\{[\s\S]{0,400}filter:\s*blur\(6px\)/.test(html) &&
  /\.hc\.reveal\{[\s\S]{0,400}filter:\s*blur\(8px\)/.test(html));
check('reveal_duration_longer_900ms',
  /\.hc\.reveal\{[\s\S]{0,400}transition:[\s\S]{0,200}900ms/.test(html));

// ─── Final ─────────────────────────────────────────────────────────────────
console.log('\n— Summary: ' + pass + ' passed, ' + fail + ' failed');
process.exit(fail === 0 ? 0 : 1);
