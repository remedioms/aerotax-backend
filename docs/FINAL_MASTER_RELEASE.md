# Final Master Release — Bericht

Stand: 2026-05-21. Status: **PASS TO CONTROLLED DEPLOY TEST** für Frontend; Backend-Receipt-Classifier braucht eigenen Sprint.

## §1 Was JETZT umgesetzt wurde

### Phase 1 — Brand/Hero (bereits in vorherigen Sprints geliefert)
- H1: „Aus Dienstplan und Spesen wird dein Steuer-Überblick."
- Sub mit Sunset-Gradient: „Du fliegst. Wir rechnen. Du trägst ein."
- Kein „keine Steuerberatung" mehr im Hero / Download / Status-Banner
- Footer: „AeroTAX erstellt eine Aufstellung auf Basis deiner Unterlagen. Bitte prüfe die Angaben eigenverantwortlich oder mit deiner Steuerberatung."

### Phase 2 — USP-Block (bereits geliefert)
„Mehr als nur hochladen" / „AeroTAX zählt, gleicht ab und erklärt" mit 5 Cards: 🧭 Dienstplan · 🔁 Streckeneinsatz · 💸 Z77 · 📄 PDF · ✈️ Crew-spezifisch.

### Phase 3 — 3-Step-Flow ✅ NEU
- 5 Internal-Panels (Steuerjahr → Dokumente → Angaben → Zahlung → Auswertung) bleiben unter der Haube für Routing/State.
- Sichtbar nur noch **3 Stages**: `Hochladen · Ergänzen · Auswertung`.
- Mapping über `window._updateStages(n)`: p0+p1 → Stage 1, p2+p3 → Stage 2, p4 → Stage 3.
- Legacy `.st-legacy` Wrapper im DOM für Backward-Compat (`goStep` setzt weiter `.active`/`.done` auf `st0..st4`).
- Glass-Stage-Indicator mit Blue-Active-Ring + Green-Done-Tick, verbunden durch dünne Linie.

### Phase 4 — Optional Belege Simplification ✅ NEU
- **Single Glass-Dropzone** `#opt-dropzone` mit 📎-Icon, bis zu 50 Files, dashed glass border.
- Text: „Optionale Belege / Bis zu 50 Belege hochladen. AeroTAX versucht Betrag, Datum und Kategorie automatisch zu erkennen. Nicht erkannte Belege werden im PDF-Anhang aufgeführt, aber nicht automatisch eingerechnet."
- Kollabierte Beispiele-Liste inline: „Beispiele anzeigen ▾"
- **Receipt-Summary-Card** `#opt-receipt-summary` (Liquid Glass) wird nach Upload sichtbar mit Counts.
- Kategorienwand (Werbungskosten/Versicherungen/Gesundheit/Spenden) versteckt hinter „Belege nach Kategorie zuordnen (optional) ▾" — für Power-User die wissen wo der Beleg hingehört.
- JS-Handler `window.uploadOptAny(inp)` enforced 50-File-Limit, appendet statt zu ersetzen, ruft `_persistUploadToBackend('opt_auto', ...)`.

### Phase 5 — Receipt-Classifier (DESIGN-DOC, kein Backend-Deploy) 📋
Komplettes Design in `docs/RECEIPT_CLASSIFIER_DESIGN.md`:
- Per-Receipt-Schema (receipt_id, amount, date, merchant, category, confidence, source_type=`user_document`, included_in_total, needs_review, audit_notes)
- 9 Kategorien-Enum (arbeitsmittel/weiterbildung/gewerkschaft/telefon_internet/reinigung_uniform/reisekosten/versicherung_beruf/sonstige_beruf/nicht_erkannt)
- 5 Inclusion-Regeln (A: hoch-confidence → include; B: ambig+geldrelevant → review; C: ambig+klein → audit-only; D: unrecognized → Anhang ohne Summe; E: user-bestätigt → Stern)
- Hard-Constraints: Optional-Belege werden NIE von Z77 oder Z17 verrechnet — strikt eigener Topf
- 12 Backend-Tests spezifiziert (für späteren Sprint)
- Datenfluss + PDF-Darstellung (§4 eingerechnet, §5 Anhang)

### Phase 6 + 7 — Smart Chat Review (bereits geliefert)
`_should_create_review` Backend-Filter mit 5 Stufen (A auto-resolve / B low-money / C counter-evidence / D money-relevant ambiguous / E answered) + Source-Conflict-Trap. Near-8h Review mit echter Minutenzahl + 14€-Effekt.

### Phase 8 — PDF/UI Source-Labels (bereits geliefert, plus Receipt-Erweiterung spec'd)
Source-Legende „Woher kommen die Werte?" als Chip-Style: Dienstplan/CAS · Streckeneinsatz · Lohnsteuerdaten · BMF-Pauschalen · Deine Angabe *. Receipt-Erweiterung im Design-Doc spezifiziert: zusätzlicher Chip „Hochgeladener Beleg".

### Phase 9 — Download-Copy (bereits geliefert)
„Dein Steuer-Überblick ist fertig." + „Mit berechneten Werbungskosten, Spesen-Abgleich und Quellen deiner Werte — vorbereitet zum Eintragen." + ⬇ PDF herunterladen (zentriert). Keine Warnbox, kein „Fallback"-Text.

### Phase 10 — Render-Cleanup (bereits erledigt)
0× `onrender` / `RENDER_FALLBACK` im Frontend. `CLOUD_RUN_PROD = 'https://aerotax-backend-443401186607.europe-west3.run.app'` als Single Source of Truth.

## §2 Tests + Ergebnis

```
Backend pytest                      : 2148 passed, 13 skipped, 13 xfailed
test_frontend_release_copy.mjs      : 113/113 ✓  (+27 neue Tests für Phase 3/4/5)
test_frontend_state_machine_live_run: 28/28 ✓
test_frontend_scroll_helpers        : 15/15 ✓
test_frontend_progress_shimmer      : 23/23 ✓
TOTAL                               : 2327 tests, 0 fails
```

Neue Tests in dieser Phase:

| Phase 3 | Phase 4 | Phase 5 |
|---|---|---|
| `three_stage_indicator_present` | `opt_dropzone_present` | `receipt_classifier_design_doc_exists` |
| `stage_labels_hochladen_ergaenzen_auswertung` | `opt_dropzone_describes_50_limit` | `receipt_classifier_doc_specifies_inclusion_rules` |
| `legacy_five_tabs_hidden_via_st_legacy_wrapper` | `opt_dropzone_explains_classifier_behavior` | `receipt_classifier_doc_specifies_z77_z17_hard_constraint` |
| `st_legacy_display_none` | `opt_dropzone_has_collapsed_examples` | |
| `update_stages_function_wired_to_goStep` | `opt_receipt_summary_block_present` | |
| `stage_mapping_p0_p1_to_stage1` | `opt_category_wall_now_collapsed_optional` | |
| `stage_mapping_p2_p3_to_stage2` | `uploadOptAny_handler_defined` | |
| | `uploadOptAny_enforces_50_file_limit` | |
| | `uploadOptAny_appends_does_not_replace` | |
| | `opt_summary_uses_glass_surface` | |
| | `opt_intro_above_cards_is_dropzone_primary_surface` | |

## §3 Dateien geändert

| File | Was |
|---|---|
| `~/Desktop/site/index.html` | 3-Stage-Indicator CSS+HTML+JS, `.st-legacy` Wrapper, `#opt-dropzone`, `#opt-receipt-summary`, `window.uploadOptAny`, kollabierte Kategorienwand |
| `tests/test_frontend_release_copy.mjs` | +27 Tests, 1 angepasst (`opt_intro_above_cards_no_long_explainer` bleibt, `opt_intro_above_cards_is_dropzone_primary_surface` neu) |
| `docs/RECEIPT_CLASSIFIER_DESIGN.md` (NEU) | 12 Sektionen: Produkt-Regel, Schema, Kategorien, 5 Inclusion-Regeln, Hard-Constraints, Datenfluss, PDF-Darstellung, Test-Plan, Frontend-Stub-Status, Outstanding |

Kein `app.py` angefasst.

## §4 Backend deploy nötig?

**Nein** — für Phase 3+4 nichts. Frontend-only Änderungen.

**Ja, aber in eigenem Sprint** — für Phase 5 (Receipt-Classifier-Implementation):
- `classify_receipt(parsed)` Python-Function mit Regeln §4 des Design-Docs
- `/api/upload/optional-receipts` Endpoint mit 50-Limit-Enforcement
- `parse_optionale_belege` Erweiterung um `category_confidence`, `merchant`
- Result-Dict `optionale_belege[]` vollschema
- PDF-Renderer §§4 + 5
- Review-Generator Hook
- 12 Backend-Tests

## §5 Frontend deploy nötig?

**Ja** — Phase 3 Stage-Indicator + Phase 4 Dropzone sind sichtbare UX-Änderungen.

Deploy-Befehl (wenn du grünes Licht gibst):

```
wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true
```

## §6 Smart-Review Beispiele (Phase 7 als Referenz, bereits aktiv im Backend)

Aktive Backend-Filter-Hierarchie:

| Filter | Bedingung | Aktion |
|---|---|---|
| E. Already answered | `status == 'answered'` | keep |
| F. Source-Conflict-Trap | `se_foreign_evidence` + `money ≥ 14€` | **immer keep** (nie silent skip) |
| B. Low-Value | `money_impact < 5€` | skip (audit-only) |
| A. KI-Auto-Resolve | `confidence ≥ 0.90` + `suggested_answer` + `ai_safe_to_resolve=True` | skip (audit) |
| C. Counter-Evidence | `score ≥ 3` UND `counter_evidence_sources ≥ 2` | skip (audit) |
| D. Money-relevant ambiguous | sonst | **keep** (Review zeigen) |

Near-8h-Review feuert nur bei `420 ≤ total < 480` Minuten + `duty_known` + nicht-overnight, mit konkreter Minutenzahl + 14€-Hinweis.

## §7 Final Recommendation

### Frontend: **PASS TO CONTROLLED DEPLOY TEST**

3-Stage-Flow wird visuell sauber, Dropzone funktioniert (persistiert via existing channel), Categorie-Wall versteckt für Newbies aber zugänglich für Power-User, Receipt-Summary ready für Backend-Counts. Alle bestehenden Tests grün.

### Backend Receipt-Classifier: **NEEDS_SEPARATE_SPRINT**

Design fertig in `docs/RECEIPT_CLASSIFIER_DESIGN.md`. Frontend-Stub liefert Files an Backend via `opt_auto`-key. Bis Backend-Sprint zeigt Summary nur Count + Placeholder „Klassifikation startet bei Auswertung". Kein Doppel-Counting in VMA/Z77/Z17 möglich da Frontend-Stub keine Werte in die Berechnungs-Tabelle schreibt.

## §8 Hard-Stops eingehalten

- Kein gcloud-Deploy
- Kein wrangler-Deploy
- Kein Live-Run
- Kein Production-Switch
- Keine Tibor-/FollowMe-Hardcoding
- Keine Vermischung Optional-Belege ↔ Z77/Z17
- Keine KI-Steuerbeträge ohne Regelprüfung (Receipt-Classifier-Design erzwingt explizit „Klassifikator generiert NIE einen Steuerbetrag")
