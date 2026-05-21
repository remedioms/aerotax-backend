# Frontend Live-Run UX + State Fix — 2026-05-20

Bug-Report: Live-Test mit Token `AT-11CEB21120E7799B` zeigte:

1. „Status wird geprüft" / „STATUS WIRD GEPRÜFT" parallel zur Result-Card.
2. Beim Klick auf „Weiter" wurde die aktive Step-Card nicht zuverlässig zentriert.
3. Nach Auswertung sprang die Seite nach oben statt beim Ergebnis zu bleiben.
4. Progress-Animation wirkte am Ende „eingefroren" bei ca. 92 %.

Diese Doku fasst Root-Cause, Fixes, Tests und Manual-QA-Checkliste zusammen.

---

## 1. Root Cause „Status wird geprüft"

Backend `/api/session/<token>` für `AT-11CEB21120E7799B` lieferte:

| Feld | Wert |
|---|---|
| `canonical_state` | (fehlt) |
| `status` | (fehlt) |
| `pdf_allowed` | (fehlt) |
| `user_title` | (fehlt) |
| `next_actions` | (fehlt) |
| `review_items` (top) | (fehlt) |
| `result_data._review_items` | `[{status: 'pending', type: 'unknown_marker', q: 'RB-Kennung…'}]` |
| `result_data.netto / brutto / arbeitstage` | `976.0 / 52884.81 / 135` |
| `download_url` | `/api/download/fc1cef9f-…` |

Zwei Compound-Bugs:

**Bug A — Frontend `calculate()` Polling-Done-Path (Primary).**
`index.html` ehemals `~3577-3583`:
```js
_data = { ...data.data, download_url, abrechnungen, optionale_belege, notes };
```
Die top-level State-Felder aus `/api/job/<id>` (`canonical_state`, `status`,
`pdf_allowed`, `user_title`, `user_message`, `next_actions`, `review_items`,
`document_health`, `retry_allowed`) gingen verloren. `render(_data)` →
`deriveUiState(_data)` sah kein `canonical_state` und fiel in den `'unknown'`-
Fallback → `banner_title = 'Status wird geprüft'` parallel zur Result-Card.

**Bug B — Backend Deploy-Lag (Secondary).**
Aktuelles `app.py:5887` setzt `safe.update(_classify_job_state(job, s))`. Der
Live-Deploy auf Render reflektiert das aber noch nicht — auch die 404-Antwort
(`/api/session/__nonexistent__`) liefert nur `{error: '…'}` statt der State-
Felder. Auto-Resume / Recall-Paths sind durch `_normalizeBackendState` schon
abgesichert; der Calculate-Poll-Path war es bisher nicht.

---

## 2. Token-Repro AT-11CEB21120E7799B

| Backend-Feld | Live-Wert | Frontend-Erwartung | Ist-UI (vor Fix) |
|---|---|---|---|
| `canonical_state` | (missing) | `needs_review` (1 pending) | `unknown` → „Status wird geprüft" |
| `pdf_allowed` | (missing) | `false` (review locked) | undefined → PDF gelockt |
| `user_title` | (missing) | „Auswertung vorbereitet — kurze Klärung nötig" | „Status wird geprüft" |
| `next_actions` | (missing) | `[{open_review_chat},{support}]` | `[]` |
| `review_items` (top) | (missing) | 1 pending | empty top-level |

Result-KPIs vs. Reference-Contract (preliminary, RB noch offen):

| KPI | Reference | Live | Δ | Tolerance | Verdict |
|---|---:|---:|---:|---:|---|
| fahrtage | 53 | 55 | +2 | ±2 | borderline ✓ |
| arbeitstage | 129 | 135 | +6 | ±3 | +3 over ⚠ |
| hotel_naechte | 54 | 73 | +19 | ±3 | +16 over ⚠ |
| gesamt | ~5743 | 5339 | −404 | ±250 | −154 over ⚠ |

Anmerkung: Steuerfreie Spesen 4705 € > VMA-Pauschalen 4363 € → AG-Erstattung
verrechnet fast komplett → niedriges `netto=976€`. Nach RB-Antwort wird neu
gerechnet — Hotel/Arbeitstage-Δ kann sich dabei verschieben.

---

## 3. Fix — `calculate()` Polling-Done-Path

`site/index.html` (`calculate()`-Done-Branch, ehemals 3577-3583, jetzt erweitert):

- top-level State-Felder (`canonical_state`, `reason_code`, `user_title`,
  `user_message`, `next_actions`, `pdf_allowed`, `review_items`,
  `document_health`, `retry_allowed`, `session_token`) werden in `_data` gemerged.
- Direkt danach `_normalizeBackendState(_data)` als Defensive (Deploy-Lag,
  Race-Window).

---

## 4. Fix — `render()` Defensive Normalize

`render(d)` enthält jetzt einen Idempotent-Guard direkt am Entry:

- wenn `d.canonical_state` fehlt UND Top-Level-Result-Signale vorhanden sind
  (`netto>0`/`brutto>0`/`arbeitstage>0`), wird `result_data` synthetisch gefüllt
  und durch `_normalizeBackendState(...)` geleitet.
- Felder werden nur gesetzt, wenn sie noch nicht existieren (kein Override
  vorhandener Backend-Werte).

Damit ist `unknown`-Fallback praktisch unerreichbar, sobald irgendein Result
da ist.

---

## 5. Fix — Scroll / Centering

Neue Helfer in `site/index.html` (nahe `_scrollToTool`):

- `window._scrollToActivePanel(reason)` — reason ∈ `step|result|error|review|processing|tool`
- `window._centerActiveCard(el, opts)`
- `window._prefersReducedMotion()`, `window._userIsTyping()`

Regeln:
- `requestAnimationFrame` nach DOM-Mutation, damit Layout settled ist.
- `behavior: 'smooth'` per default, `'auto'` bei `prefers-reduced-motion`.
- Kein erzwungener Scroll, wenn `document.activeElement` Input/Textarea/
  contentEditable ist (User tippt im Chat) — außer `force=true` (result/error).
- Debounce ≥250 ms zwischen zwei Center-Aktionen außer `force`.
- Vertikale Zentrierung; bei `elH > 0.9 * vh` Header-Offset 6 %.

Ersetzungen:
- `render()` Done-Path: `window.scrollTo(0,0)` → `_scrollToActivePanel('result|review|error')` je nach `canonical_state`.
- `showCalculationError()`: `window.scrollTo({top:0})` → `_scrollToActivePanel('error')`.
- `go(n)` Step-Wechsel: `document.getElementById('tool').scrollIntoView(...)` → `_centerActiveCard(activePanel)`.

---

## 6. Fix — Glass-Card-Transition

Neue CSS-Klasse `.panel-entering`:

```css
.panel.panel-entering{
  animation: panelGlassIn 220ms cubic-bezier(.22,1,.36,1) both;
  will-change: opacity, transform;
}
@keyframes panelGlassIn{
  from { opacity:0; transform:translateY(10px) scale(.985); filter:blur(.5px); }
  to   { opacity:1; transform:translateY(0)    scale(1);    filter:blur(0); }
}
@media (prefers-reduced-motion: reduce){
  .panel.panel-entering{ animation: none; }
}
```

Hook in `go(n)`:
- altes Panel `.active`/`.panel-entering` entfernt.
- neues Panel `.active` + (nach `requestAnimationFrame`) `.panel-entering`.
- `animationend`-Listener entfernt die Klasse wieder + Safety-Cleanup nach 400 ms.

Dauer: 220 ms (innerhalb der 180-280 ms Richtlinie). Kein Layout-Sprung, kein
stuck.

---

## 7. Fix — Progress 92 %-stuck

Symptom: Progress-Bar war bei ca. 92 % gedeckelt und wirkte statisch, weil die
Heartbeat-Phase nur noch 0.15 % pro Iteration drauflegen konnte.

Fix in `startStatusAnimation()` (Heartbeat-Loop):

1. Sobald die Bar ≥91.5 % erreicht → CSS-Klasse `pf-indeterminate` wird auf
   `#pf` gesetzt → Shimmer-Overlay läuft kontinuierlich über die Bar
   (`@keyframes pfShimmer 1800ms infinite`). Bar bleibt visuell bei 92 %, aber
   „lebt" sichtbar weiter.
2. `heartbeatStart`-Timestamp wird mitgezählt. Eskalations-Messages:
   - **>90 s**: „Die Auswertung läuft noch. Das kann bei vielen Dokumenten ein paar Minuten dauern."
   - **>300 s (5 min)**: „Die Auswertung läuft weiter. Du kannst mit deinem Zugangscode später zurückkommen."
3. Sobald `calculate()` resolved und die Bar auf 100 % springt → `pf-indeterminate`
   wird wieder entfernt; Shimmer stoppt.
4. `prefers-reduced-motion: reduce` → Shimmer-Animation `none`, statt dessen
   leise Opacity 0.35.

Damit keine falsche „100 %-fertig"-Wirkung vor finalem State, kein statisches
Einfrieren, ehrliche Kommunikation bei Long-Running.

---

## 8. Tests

`tests/test_frontend_state_machine_live_run.mjs` — 28 / 28 grün.

Cases:
- 1: Bug-Repro (no `canonical_state`, result_data + pending review) → `needs_review`, banner ≠ „Status wird geprüft".
- 2: result_data only → `done`, PDF erlaubt.
- 3: empty → `processing`.
- 4: `fetch_error=true` → `fetch_error`.
- 5: `status=failed_*` → `failed_retryable`.
- 6: error-Feld → `failed_retryable`.
- 7: top-level review_items hat Vorrang über inner `_review_items`.
- 8: explizites Backend-`canonical_state` wird nicht überschrieben.
- 9: resolved/skipped review items → `done`.
- 10: Bug-Shape erzeugt nie `status_kind='unknown'`.
- 11: Review-only ohne Result → `needs_review`.
- 12: `null`/`undefined` survives.

Backend-Pytest (Rel Phase 11): `tests/test_release_frontend_state_machine.py` — bestehend, betroffen nicht negativ.

---

## 9. Manual QA Checklist

Vor jedem Deploy folgende Browser-Matrix durchspielen.

### Chrome Desktop
- [ ] Schritt 1 → 2 → 3: jede Step-Card landet zentriert im Viewport, keine Sprünge.
- [ ] Glass-Card-Transition sichtbar (~220 ms fade+scale), kein Layout-Sprung.
- [ ] Processing: Progress steigt phasenweise bis ~88 %, dann Shimmer.
- [ ] >90 s: Long-Running-Message erscheint.
- [ ] >5 min: Zugangscode-Hinweis erscheint.
- [ ] Done: Result-Card sichtbar zentriert, kein Jump-to-Top.
- [ ] PDF-Button sichtbar nur bei `pdf_allowed=true` + `download_url`.

### Safari Desktop
- [ ] gleiche Schritte; `-webkit-` Vendor-Prefixes funktional.
- [ ] Smooth-Scroll feuert.

### iPhone / Mobile Safari
- [ ] Step-Wechsel: aktive Card sichtbar nach „Weiter".
- [ ] Soft-Keyboard verdeckt Input nicht (Chat fokussiert: scroll wird unterdrückt).
- [ ] Glass-Transition ruckelfrei.
- [ ] Progress-Shimmer sichtbar.

### Hard-Reload
- [ ] Während Processing: nach Reload Status-Banner sofort sichtbar (no state-mix).
- [ ] Während Done: keine doppelten Messages, Result-Panel zentriert.

### Recall-Token
- [ ] `AT-…` einlösen → Result-Card erscheint zentriert.
- [ ] `canonical_state=needs_review` → kein „Status wird geprüft"-Banner mehr.
- [ ] `pdf_allowed=true` + `download_url` → Download-Button sichtbar.

### Long-Running (>5 min)
- [ ] Progress-Bar Shimmer läuft, keine statische 92 %.
- [ ] Eskalationsmessage Stufe 1 (>90 s), Stufe 2 (>5 min).
- [ ] Token-Card sichtbar mit Hinweis „später zurückkommen".

### Needs-Review
- [ ] Banner-Title „Auswertung vorbereitet — kurze Klärung nötig".
- [ ] Chat-Panel sichtbar, PDF-Button gelockt.
- [ ] `show_final_amount=false` (kein finaler Betrag).

### Failed
- [ ] Error-Banner sichtbar + Retry/Support-Buttons.
- [ ] Kein PDF-Button.
- [ ] Kein „Done + Failed"-Mix.

### prefers-reduced-motion
- [ ] Glass-Animation `none`.
- [ ] Shimmer ersetzt durch leise statische Opacity.
- [ ] Scroll `behavior: 'auto'`.

---

## 10. Remaining Risks

1. **Backend Deploy-Lag** — `/api/session/<token>` liefert canonical_state in der Live-Deploy noch nicht mit. Frontend kompensiert per Normalize, aber sauberer wäre ein Backend-Re-Deploy. Kein Blocker.
2. **Hotel/Arbeitstage Δ > Tolerance** — Token-Result zeigt +19 Hotelnächte / +6 Arbeitstage vs. Reference-Contract. Erwartung: nach RB-Marker-Antwort + Recalc shifted. Live-Verifikation nach Phase 2-Deploy + Token-Re-Run nötig.
3. **Mobile Soft-Keyboard** — Helper unterdrückt erzwungenen Scroll bei aktiver Input-Focus, aber explizite Tests auf iPhone-Safari stehen aus.
4. **Touch-Drag Scroll Conflict** — Wenn User während Smooth-Scroll manuell scrollt, kann es zu doppeltem Scroll-Verhalten kommen. Debounce 250 ms mitigiert, ist aber nicht 100 % ausgeschlossen.

---

## 11. Recommendation

**Ready für Frontend-Deploy-Test** mit Token `AT-11CEB21120E7799B`:
- State-Mix Bug ist gefixt + getestet.
- Scroll/Centering + Glass-Transition + Progress-Shimmer + Eskalation umgesetzt.
- Lokale Tests 28/28 grün, kein Syntax-Fehler.
- Hard-Stop-Rules eingehalten: kein Deploy, kein Live-Run, kein Production-Switch.

Vor User-Freigabe für Live-Run sollte Phase 4B (Progress) und Phase 3 (Scroll)
in echtem Browser einmal manuell durchgegangen werden (siehe §9). Dann
`wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true`
durch User-Trigger.
