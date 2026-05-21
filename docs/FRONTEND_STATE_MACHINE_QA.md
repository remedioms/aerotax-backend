# AeroTAX — Frontend State Machine QA

Stand: 2026-05-20. Pflicht-States + Akzeptanz für Launch-Readiness.

## §1 Canonical States

Frontend nutzt `deriveUiState()` aus `~/Desktop/site/index.html`. Backend liefert
`canonical_state` per `/api/job/<id>`. Pflicht-States:

| canonical_state | Pflicht-UI | Verboten |
|---|---|---|
| `processing` | „Wird ausgewertet" Spinner, evtl. Phase-Hinweis | KEINE Done-Details, KEIN PDF, KEIN Chat (außer expliziter Hinweis), KEIN „Status wird geprüft" als Dauerzustand |
| `needs_review` | Review-Items, Chat-Drawer, „Klärung nötig"-Banner, **live-Betrag im Chat-Header (Polish-Workaround)** | KEIN „Auswertung abgeschlossen", KEIN PDF-Download, KEINE Fehler-Banner |
| `done` | Betrag, PDF-Download, Chat optional, Result-Card komplett | KEINE Fehler-Banner, KEINE Review-Blocker |
| `failed_retryable` | Fehlermeldung mit Reason, Retry-Button, Support-Hinweis | KEINE Beträge, KEIN PDF, KEINE Done-Sections |
| `failed_terminal` | Klare „Auswertung fehlgeschlagen"-Meldung, Support-Link | KEINE Beträge, KEIN PDF, KEINE Done-Sections |
| `expired` | „Token abgelaufen / Session vorbei", Neu-Login-Hinweis | KEINE Result-Sections |
| `deleted` | „Daten wurden gelöscht", Cleanup-Hinweis | KEINE Result-Sections |

## §2 State-Tests (Pflicht)

`tests/test_phase3a_*.py`, `tests/test_pdf_button_gradient.py`, `tests/test_e2e_simulator_review.py`:

| Test | Akzeptanz |
|---|---|
| `test_no_failed_done_mix` | Bei `failed_*` werden KEINE done-Sections gerendert |
| `test_no_needsreview_done_mix` | Bei `needs_review` KEIN „done"-Heading, KEIN PDF-Button |
| `test_no_status_pruefen_dauerzustand` | Wenn `canonical_state` gültig (≠ unknown): KEIN „Status wird geprüft"-Loop |
| `test_canshowpdfdownload_only_done_with_url` | `canShowPdfDownload` nur bei `state=done AND pdf_allowed=true AND pdf_url present` |
| `test_hard_reload_recall_correct` | Hard-Reload (F5) während `processing`: Auto-Resume mit Token, kein Reset |
| `test_safari_reload_correct` | Safari-spezifischer Hard-Reload (Cache-Buster `_v=...`) |
| `test_auto_resume_banner_at_reload` | Recall-Banner wenn `localStorage.token` gesetzt + Job aktiv |

**Aktueller Status (Regression 1521)**: alle 7 Tests grün (Phase 3A/3B/3D Stand).

## §3 deriveUiState() Contract

Pflicht-Function in `index.html`:
```js
function deriveUiState(stateFromApi, pdfAllowed, pdfUrl, reasonCode) {
  // returns { showProcessing, showReview, showDone, showFailed,
  //          showExpired, showDeleted, canShowPdfDownload, errorMessage }
}
```

**Pflicht-Invarianten**:
- Genau EIN `show*`-Flag = true zu jeder Zeit (Mutual Exclusion)
- `canShowPdfDownload` nur true wenn `showDone === true` UND `pdfAllowed === true` UND `pdfUrl` vorhanden
- `errorMessage` nur bei `showFailed/showExpired/showDeleted`

## §4 v8.23 Release-Stubs (offene, ehrlich kommuniziert)

Aus CLAUDE.md §1:
1. **Document-Replacement selektiver Re-Read** — `pending_reread=True` blockiert `/finalize-pdf`. UI zeigt „Datei erhalten. Erneute Auswertung noch nicht abgeschlossen." (KEINE Behauptung „Auswertung aktualisiert".)
2. **Marker-Lexikon Klassifikator-Integration** — `_record_marker_learning` pflegt `marker_lexicon.json`, aber Klassifikator nutzt approved-Marker NICHT aktiv beim nächsten Job. User-facing: „Für diese Auswertung berücksichtigt. Als Lernkandidat gespeichert." (KEIN „Beim nächsten Mal automatisch erkannt".)
3. **Side-Drawer ohne parallel sichtbare Result-Card** — Chat als rechts-fixierter Glassmorphism-Drawer (Desktop) / Modal (Mobile). **live-Betrag im Chat-Header** als Polish-Workaround.

**Akzeptanz**: Forbidden-String-Audit (Phase 3C) prüft, dass diese Stubs ehrlich kommuniziert sind, keine falschen Versprechungen.

## §5 Open-Concerns / Phase-M-Closure-TODO

| Item | Status | Pflicht vor Launch? |
|---|:-:|:-:|
| Hard-Reload-Test Safari iOS Mobile | ⚠ manual QA needed | ja |
| Hard-Reload-Test Chrome macOS | ⚠ manual QA needed | ja |
| Recall-Token-Flow nach 24h | ⚠ manual QA | ja |
| Drawer-Open + Live-Betrag-Polish | ✓ implementiert | nein |
| `canShowPdfDownload` zentrale Funktion | ✓ Phase 3A | nein |
| Forbidden-String-Audit | ✓ Phase 3C | nein |
| Demo-Isolation | ✓ Phase 3C | nein |

**Empfehlung**: Vor Launch ein manueller Browser-QA-Lauf auf 4 Geräten/Browsers gegen die State-Tabelle in §1.
