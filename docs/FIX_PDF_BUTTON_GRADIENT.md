# PDF-Button Gradient AeroTAX — Analyse + Fix-Plan

**Datum:** 2026-05-14
**Scope:** Frontend-only. Kein Backend. Kein Live-Run.
**Status:** Analyse vor Diff. **Kein Code geändert.**

---

## 1. AeroTAX-Gradient-Referenz

**CSS-Variable (Z.39):**
```css
--grad: linear-gradient(135deg, #ea7a3c 0%, #e25a96 40%, #8060d8 70%, #3b6cd6 100%);
```
(orange → pink → violet → blue)

**Bereits korrekt im Code:**
- `.dlb` CSS-Klasse (Z.996, Z.1393) nutzt `background: var(--grad) !important` — das ist die kanonische Brand-Klasse für Download-Buttons
- Hero-Headline (Z.1941) nutzt 4-Stop-Gradient
- `--grad` ist die single-source-of-truth

---

## 2. Alle PDF-Download-Button-Stellen (Result-Card + Chat)

| # | Stelle | Datei:Zeile | Aktueller Style | State | Aktion |
|---|---|---|---|---|---|
| 1 | `#header-pdf-btn` | index.html:2365 | `class="dlb"` → **bereits Gradient** ✓ | done | **unverändert** |
| 2 | `#dl-btn-main` (Bottom-Fallback) | index.html:2517 | inline `linear-gradient(135deg,#3b82f6,#8b5cf6)` (BLAU-LILA — falsch) | done | **fix** |
| 3 | chat-pdf-cta `_refreshPdfBubble` btn | index.html:4942 (in JS-template) | inline `linear-gradient(135deg,#3b82f6,#8b5cf6)` (BLAU-LILA — falsch) | done | **fix** |
| 4 | `next_actions` Renderer `download_pdf`/`create_pdf` | index.html:3676-3680 | inline gemixt mit `retry/start_new` → blau-lila | done/needs_review | **fix (split)** |
| 5 | `#pdf-locked-indicator` (info-banner) | index.html:2454 | yellow-tinted, KEIN Button | locked | **unverändert** — locked bleibt locked, NICHT gradient |

**State-Sicht:**
- Stelle 1 (`#header-pdf-btn`) ist gegated über `_applyPdfVisibility` + `canShowPdfDownload` → erscheint NUR bei `state=done` + Backend `pdf_allowed=true` + `download_url` da
- Stelle 2 (`#dl-btn-main`) gleicher Gate
- Stelle 3 (chat-bubble) hat eigenen Gate: `_refreshPdfBubble` returnt early wenn `!canShowPdfDownload(window._data)` (Z.4922-4925)
- Stelle 4 (next_actions) wird nur gerendert wenn Backend `next_actions` enthält `download_pdf`/`create_pdf` — Backend liefert das nur in `done`-State

→ **Locked/needs_review** bekommen aktuell schon keinen aktiven PDF-Button. Wir müssen NUR die Gradient-Farbe für die done-Buttons korrigieren.

---

## 3. Falsche blau-lila Buttons (NICHT-PDF) — out of scope

Gleiche `#3b82f6,#8b5cf6` Gradient-Farbe findet sich auch in:
- `#hero-primary-btn` (Z.2426) — "Chat öffnen"
- `.review-btn-submit` (Z.4015)
- Quick-Chips (Z.4181)
- yes/no Buttons (Z.4556, Z.4767)
- Retry-Code (Z.5086)
- Modal OK (Z.5131)

**Explizit out-of-scope per User-Anweisung „nur PDF-Download-Buttons".** Werden NICHT angefasst.

---

## 4. Fix-Plan (Diff)

### Change 1: `#dl-btn-main` (Z.2517) — Klasse `dlb` nutzen, blau-lila raus

**Vorher:**
```html
<button onclick="dlPDF()" id="dl-btn-main" style="display:none;padding:10px 20px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;border:none;border-radius:980px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(59,130,246,0.28);align-self:flex-start;margin-top:2px;">⬇ PDF herunterladen</button>
```

**Nachher:**
```html
<button onclick="dlPDF()" id="dl-btn-main" class="dlb" style="display:none;padding:10px 20px;border-radius:980px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(234,122,60,.32);align-self:flex-start;margin-top:2px;">⬇ PDF herunterladen</button>
```

- `class="dlb"` zugewiesen → `background: var(--grad) !important` greift
- `background:` + `color:white;border:none` aus inline-style raus (CSS-Klasse macht das)
- `box-shadow` auf Orange-Tönung angepasst (passend zum Gradient-Start)
- Sichtbarkeits-Gate (`display:none` + sich über JS togglen) bleibt unverändert

### Change 2: `_refreshPdfBubble` Button (Z.4942) — `var(--grad)`

**Vorher:**
```javascript
btn.style.cssText = 'padding:10px 20px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;border:none;border-radius:980px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(59,130,246,0.28);align-self:flex-start;margin-top:2px;';
```

**Nachher:**
```javascript
btn.style.cssText = 'padding:10px 20px;background:var(--grad);color:white;border:none;border-radius:980px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(234,122,60,.32);align-self:flex-start;margin-top:2px;';
```

- Inline `linear-gradient(...)` durch `var(--grad)` ersetzt
- Box-shadow Orange-Tönung
- Rest unverändert

### Change 3: next_actions Renderer (Z.3676-3680) — split PDF vs retry/start_new

**Vorher:**
```javascript
var isPrimary = (act.type === 'retry' || act.type === 'start_new' || act.type === 'download_pdf');
btn.style.cssText = isPrimary
  ? 'padding:10px 20px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;border:none;border-radius:980px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(59,130,246,.28);'
  : 'padding:10px 20px;background:rgba(255,255,255,.08);color:rgba(255,255,255,.9);border:1px solid rgba(255,255,255,.16);border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;';
```

**Nachher:**
```javascript
var isPdfAction = (act.type === 'download_pdf' || act.type === 'create_pdf');
var isPrimary = (act.type === 'retry' || act.type === 'start_new');
if(isPdfAction){
  btn.style.cssText = 'padding:10px 20px;background:var(--grad);color:white;border:none;border-radius:980px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(234,122,60,.32);';
} else if(isPrimary){
  btn.style.cssText = 'padding:10px 20px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;border:none;border-radius:980px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(59,130,246,.28);';
} else {
  btn.style.cssText = 'padding:10px 20px;background:rgba(255,255,255,.08);color:rgba(255,255,255,.9);border:1px solid rgba(255,255,255,.16);border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;';
}
```

- PDF-Aktionen (`download_pdf`, `create_pdf`) bekommen AeroTAX-Gradient
- `retry`/`start_new` bleiben **wie alt** (blau-lila) — explizit out-of-scope
- Secondary-Style (support/refresh) unverändert

### NICHT geändert

- `#header-pdf-btn` (Z.2365) — hat schon `class="dlb"` → already gradient ✓
- `#pdf-locked-indicator` (Z.2454) — yellow info-banner, kein Button, soll locked-Aussehen behalten
- 7 weitere blau-lila Buttons (hero, review-submit, quick-chips, etc.) — out of scope
- Keine CSS-Klasse-Definition geändert (`.dlb` bleibt)
- Keine neue Klasse erfunden

---

## 5. Tests

Frontend hat keine JS-Test-Runner. Stattdessen static-HTML-Audit-Pattern wie schon in `tests/test_calculation.py::test_v11_frontend_has_no_rc_dp_card` etc.

**Neue Datei:** `tests/test_pdf_button_gradient.py`

| Test | Verifiziert |
|---|---|
| `test_dl_btn_main_has_dlb_class` | `<button … id="dl-btn-main" … class="dlb"` im HTML |
| `test_dl_btn_main_no_blue_purple_gradient` | im `dl-btn-main`-Tag: kein `linear-gradient(135deg,#3b82f6,#8b5cf6)` |
| `test_refresh_pdf_bubble_uses_brand_gradient` | `_refreshPdfBubble`-Block enthält `background:var(--grad)` und KEIN `#3b82f6,#8b5cf6` |
| `test_next_actions_pdf_uses_brand_gradient` | next_actions-Renderer: `isPdfAction` → `var(--grad)` |
| `test_next_actions_pdf_split_from_retry` | next_actions: `download_pdf` und `create_pdf` NICHT mehr im `isPrimary`-Pfad |
| `test_pdf_locked_indicator_unchanged` | `#pdf-locked-indicator` bleibt yellow-tinted (`rgba(251,191,36`) und KEIN gradient-button |
| `test_header_pdf_btn_keeps_dlb_class` | `#header-pdf-btn` hat weiter `class="dlb"` |
| `test_dlb_css_class_uses_grad_var` | `.dlb` CSS-Block enthält `background: var(--grad)` (kanonische Brand-Klasse intakt) |
| `test_grad_variable_unchanged` | `:root` enthält weiter den 4-Stop-Gradient `#ea7a3c → #3b6cd6` |

Plus Regression: bestehende Tests dürfen nicht brechen (1187 baseline).

---

## 6. Deploy-Plan

| Schritt | Status |
|---|---|
| 1. Analyse zeigen | ✓ |
| 2. Du gibst Code-Diff-Freigabe | ⏸ |
| 3. Code editieren (3 Stellen, ~10 LoC netto) | ⏸ |
| 4. Tests schreiben + lokal grün | ⏸ |
| 5. Diff zeigen | ⏸ |
| 6. Du gibst Deploy-Freigabe | ⏸ |
| 7. Cloudflare-Pages-Deploy `wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true` | ⏸ |

Kein Live-Run. Kein Tibor-Run. Kein Backend.

---

## 7. Open Questions

1. **3-Stellen-Scope OK?** (dl-btn-main + chat-pdf-cta + next_actions split). header-pdf-btn schon korrekt, pdf-locked-indicator absichtlich unverändert.
2. **Box-Shadow Orange (`rgba(234,122,60,.32)`) für die 3 Stellen OK?** — passt zum Gradient-Start. Alternative: alle Brand-Stellen-Shadows beibehalten (kein Box-Shadow ändern).
3. **`retry`/`start_new` Buttons bleiben blau-lila** (out-of-scope) — bestätigen?

Nichts geändert. Warte auf Antwort.
