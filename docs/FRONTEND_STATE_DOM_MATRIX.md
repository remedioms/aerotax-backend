# Frontend State × DOM Visibility Matrix

Phase A-Inventory. Stand 2026-05-15 (post `_hardHideResultSections` + `_failedStateLocked` Deploy).

Quelle: `index.html` (`window.deriveUiState`, `window._hardHideResultSections`, `render()` non-done-gate, `showCalculationError`).

---

## States (Source-of-Truth: `canonical_state`)

| Symbol | State | Wann |
|---|---|---|
| ✓ | sichtbar | im DOM, display nicht 'none' |
| ✗ | hidden | display='none' oder Element entfernt |
| (P) | partial | Section sichtbar aber Inhalt entweder leer oder „vorläufig" |
| — | nicht definiert / nicht relevant |

### Backend Canonical-State-Werte

- `done` — Auswertung fertig, finaler Betrag, PDF erlaubt
- `needs_review` — Auswertung vorläufig, Chat + offene Punkte
- `failed_retryable` — Retry möglich (transient error)
- `failed_support` — Support nötig (terminal error)
- `fetch_error` — Frontend konnte Backend nicht erreichen
- `expired` — Code abgelaufen
- `deleted` — Session gelöscht
- `processing` / `queued` / `pending` — läuft
- `failed_local` *(Frontend-Only)* — gesetzt durch `showCalculationError` + `_failedStateLocked`
- `unknown` — Race-Window vor erstem Backend-Response

---

## Sections × State

### Header/Banner

| Element-ID | Zweck | done | needs_review | failed_* / fetch_error / failed_local | processing | expired/deleted |
|---|---|---|---|---|---|---|
| `rname` | „User — Auswertung 2025" | ✓ | ✓ | (P) „Fehler bei der Auswertung" | ✓ | ✓ |
| `rtag-year` | Banner-Text | „Auswertung abgeschlossen · Lufthansa 2025" | „Auswertung vorbereitet — kurze Klärung nötig" | „Auswertung unterbrochen" | „Auswertung läuft" | „Code abgelaufen" / „Auswertung gelöscht" |
| `state-action-buttons` | Next-action-Buttons | (P) ✓ download_pdf | (P) ✓ open_review_chat + support | ✗ hidden by `_hardHideResultSections` | ✗ | (P) ✓ retry+support |

**Setter:** `render()` Z.~3897 (rname), Z.~3669 (rtag-year via `_uiState.banner_title`).
**Cleaner:** `_hardHideResultSections` (state-action-buttons), Reset in render() Z.~3895 (`_resetTexts['rtag-year']=''`).

### Amount / Hero

| Element-ID | Zweck | done | needs_review | failed_* | processing |
|---|---|---|---|---|---|
| Hero-Wrap (`closest div[style*="background:rgba(255,255,255,.07)"]`) | Glassmorphism-Karte | ✓ | ✓ (mit „vorläufig"-Hint) | ✗ | ✗ |
| `result-amount-label` | „EINZUTRAGENDER GESAMTBETRAG" | ✓ | (P) | ✗ | ✗ |
| `result-netto-display` | Betrag „5621 €" | ✓ | (P) vorläufig | ✗ („—" weg) | ✗ |
| `result-amount-subtext` | „Zusammengefasst..." | ✓ | ✓ | ✗ | ✗ |
| `result-amount-hint` | „Aufteilung im PDF" | ✓ | ✓ | ✗ | ✗ |
| `hero-actions` | „Chat öffnen" / „Ohne Klärung fortfahren" | ✓ | ✓ | ✗ | ✗ |
| `hero-primary-btn`, `hero-secondary-btn` | Action-Buttons | ✓ | ✓ | ✗ | ✗ |

**Setter:** `render()` Done-Pfad (nach `_safe('amount_display', …)` etc).
**Cleaner:** `_hardHideResultSections` (alle obigen IDs + Hero-Wrap via `.closest()`).

### Token Card

| Element-ID | Zweck | done | needs_review | failed_* | processing |
|---|---|---|---|---|---|
| `result-session-token` | Karte mit Schnellfragen + Code + Datenschutz | ✓ | ✓ | ✓ (User braucht Code für Retry) | (siehe `proc-token-card`) |
| `result-token-display` | AT-XXX Anzeige | ✓ | ✓ | ✓ | — |

**Setter:** `render()` Z.~3976 setzt visible bei `_data.session_token`.
**Cleaner:** Bei `_hardHideResultSections({hideCodeCard:true})` (Default ist `false` — Code bleibt für Support).

### Chat

| Element-ID | Zweck | done | needs_review | failed_* | processing |
|---|---|---|---|---|---|
| `chat-inline-host` | Permanenter Inline-Chat | ✓ | ✓ | ✗ (durch `_hardHideResultSections{hideChat:true}`) | ✗ |
| `chat-messages` | Verlauf | ✓ | ✓ | ✗ | — |

**Cleaner:** Reset im render() Z.~3907 (`_chatMsgs.innerHTML=''`). Hide via `_hardHideResultSections`.

### Review Items

| Element-ID | Zweck | done | needs_review | failed_* | processing |
|---|---|---|---|---|---|
| `review-section-wrap` | Review-Karte (aktuell nur Stub, Chat ersetzt) | ✗ (display:none default) | ✗ | ✗ | ✗ |
| `floating-chat-badge` | Hidden-Stub | ✗ | ✗ | ✗ | ✗ |

### Detail-Table / Audit

| Element-ID | Zweck | done | needs_review | failed_* | processing |
|---|---|---|---|---|---|
| `details` „Berechnung im Detail" | Tabelle | ✓ | ✓ | ✗ | ✗ |
| `rtbody` | Tabellen-Body | gefüllt | gefüllt | leer | leer |
| `details` „Nachweis & Rechenweg" | | ✓ | ✓ | ✗ | ✗ |
| `audit-detail-body` | Rechenweg-Inhalt | gefüllt | gefüllt | leer | leer |
| `details` „Streckeneinsatz pro Monat" | optional | je nach Daten | je nach Daten | ✗ | ✗ |
| `section-months-wrap` | Wrapper | (P) | (P) | ✗ | ✗ |
| `audit-status-row` | Status-Pille | ✓ | ✓ | ✗ | ✗ |
| `result-notes` | Hinweise | (P) | (P) | ✗ | ✗ |

**Cleaner:** `_hardHideResultSections` versteckt alle `<details>` in `p-result` + leert `rtbody`.

### PDF Buttons / PDF Locked

| Element-ID | Zweck | done | needs_review | failed_* | processing |
|---|---|---|---|---|---|
| `header-pdf-btn` | „⬇ PDF" oben | ✓ | ✗ | ✗ | ✗ |
| `dl-btn-row` | Bottom-PDF-Bereich | ✓ | ✗ | ✗ | ✗ |
| `dl-btn-main` | „⬇ PDF herunterladen" | ✓ | ✗ | ✗ | ✗ |
| `pdf-locked-indicator` | Lock-Banner (entfernt seit 2026-05-14) | ✗ | ✗ | ✗ | ✗ |

**Gate:** `canShowPdfDownload(apiState)` — alle PDF-Visibility geht **nur** durch `_applyPdfVisibility`. Direkter `display='block'` auf PDF-Buttons ist verboten ab v14.

### Progress Panel (separate Section, kein p-result)

| Element-ID | Zweck | done | needs_review | failed_* | processing |
|---|---|---|---|---|---|
| `p-proc` | Progress-Panel | ✗ | ✗ | ✗ | ✓ active |
| `proc-token-display` | AT-XXX während laufendem Job | — | — | — | ✓ |
| `ps-current`, `ps-sub` | Live-Status-Text | — | — | — | ✓ animiert |

---

## Render-Flow

```
fetch → result_data → window._normalizeBackendState(j)
     → window.deriveUiState(normalized) → window._uiState
     → window.render(d)
        → window._applyUiState (PDF-Buttons disabled by default)
        → State-Lock-Check (window._failedStateLocked)
        → Non-Done-Gate (status_kind not in {done,needs_review}) → _hardHideResultSections + early-return
        → Reset-Texts (rtag-year, rtbody, audit-detail-body, …)
        → p-result sichtbar machen
        → Done-Pfad: header, reorder_sections, amount_display, details_table, …
```

Wichtige Funktionen:

| Funktion | Zeile | Zweck |
|---|---|---|
| `window._safeReviewPending(s)` | 1595 | Defensive Array-Check für `_review_items` |
| `window.canShowPdfDownload(s)` | 1607 | Single source-of-truth für PDF-Button-Visibility |
| `window._applyPdfVisibility(uiState)` | 1639 | Disabled-state aller PDF-Buttons |
| `window._normalizeBackendState(j)` | 1694 | Backend-State aus result_data ableiten (siehe BH-006) |
| `window._hardHideResultSections(opts)` | 1755 | Hard-Hide aller Done-Sections |
| `window.deriveUiState(apiState)` | 1801 | Banner-Title, show_*, chat_mode aus canonical_state |
| `window._applyUiState(s)` | 1685 | Apply uiState + PDF-visibility |
| `showCalculationError(msg, …)` | 7657 | Failed-State-Pin (`_failedStateLocked=true`) |
| `render(d)` | 3631 | Hauptrender (1500+ Zeilen) |
| `_autoResume` IIFE | 6620 | Initial-fetch + poll bei localStorage-Token |
| `_recallSubmit` | 8027 | Manuelle Code-Eingabe |
| `finishProcess(result, err)` | 3207 | Post-Job-Callback (catch-path) |

---

## State-Mix-Regressionen (alle in Test-Suite abgedeckt)

| Konflikt | Test | Status |
|---|---|---|
| failed + done | `test_load_failed_message_not_mixed_with_done_sections` | ✓ |
| failed lock blockt stale needs_review | `test_failed_with_stale_result_data_does_not_render_done` | ✓ |
| failed lock erlaubt explicit done | `test_failed_lock_allows_done_override` | ✓ |
| fetch_error zeigt keine Done-Sections | `test_fetch_error_does_not_show_result_sections` | ✓ |
| canonical_state=null → Normalizer | `test_normalize_backend_state_*` | ✓ |
| failed_retryable wird nicht zu done aufgewertet | `test_normalize_backend_state_does_not_upgrade_failed_to_done` | ✓ |

---

## Offene Lücken

1. **Done-Pfad rendert bei needs_review zu viel**: render() Z.~3962 prüft `_doneLike = (done || needs_review)`. Für needs_review läuft der gesamte Done-Pfad — setzt Amount-Display mit echtem Wert. Acceptance verlangt „vorläufiger Betrag erlaubt" → OK, aber kein „final" claim. Aktuell kein expliziter „vorläufig"-Marker in der UI. → siehe BH-NEW-XX (Phase B).
2. **`needs_review` Done-Pfad rendert Detail-Table mit allen Tagen**: Bei needs_review zeigt Tabelle dasselbe wie done. Acceptance: „Detail erlaubt aber als vorläufig kennzeichnen". → Phase B.
3. **`window.AEROTAX_FRONTEND_VERSION` fehlt**: kein Build-Marker → siehe BH-009.
4. **`needs_review` rtag-year**: aktuell setzt Done-Pfad rtag-year ungeprüft. Z.~3902 prüft `if(_csState === 'done')` — bei needs_review bleibt der vom Banner-Setting. OK, aber fragile.
